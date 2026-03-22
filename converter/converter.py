from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError


MAX_WEBM_SIZE = 64 * 1024
TARGET_FRAME_DURATION_MS = 30
TARGET_SIZES = (100, 96, 92, 88, 84, 80)
CRF_START = 32
CRF_STEP = 4
CRF_MAX = 60
LIGHT_TARGET_SIZES = (100,)
MEDIUM_TARGET_SIZES = (100,)
HEAVY_TARGET_SIZES = (100,)


class ConversionCancelled(Exception):
    pass


@dataclass(frozen=True)
class FrameMeta:
    index: int
    width: int
    height: int
    x_offset: int
    y_offset: int
    duration_ms: int
    dispose: str
    blend: bool


_CANVAS_RE = re.compile(r"Canvas size:\s*(\d+)\s*x\s*(\d+)")
_FRAME_RE = re.compile(
    r"^\s*(\d+):\s+"
    r"(\d+)\s+"
    r"(\d+)\s+"
    r"(yes|no)\s+"
    r"(-?\d+)\s+"
    r"(-?\d+)\s+"
    r"(\d+)\s+"
    r"(\w+)\s+"
    r"(yes|no)\s+"
    r"(\d+)\s+"
    r"(\w+)"
)


def _im_cmd() -> list[str]:
    if shutil.which("magick"):
        return ["magick"]
    return ["convert"]


def _scale_filter(target_size: int, flags: str = "lanczos") -> str:
    return (
        f"crop='min(iw\\,ih)':'min(iw\\,ih)':(iw-min(iw\\,ih))/2:(ih-min(iw\\,ih))/2,"
        f"scale={target_size}:{target_size}:flags={flags}"
    )


def _check_cancel(cancel_event=None):
    if cancel_event is not None and cancel_event.is_set():
        raise ConversionCancelled()


def _select_profile(source_size: int | None) -> tuple[tuple[int, ...], int, int, int]:
    if source_size is None:
        return LIGHT_TARGET_SIZES, CRF_START, CRF_STEP, 4
    if source_size >= 400_000:
        return HEAVY_TARGET_SIZES, 40, 6, 6
    if source_size >= 180_000:
        return MEDIUM_TARGET_SIZES, 36, 5, 5
    return LIGHT_TARGET_SIZES, CRF_START, CRF_STEP, 4


def _frame_render_limit(source_size: int | None, frame_count: int) -> int:
    if frame_count <= 0:
        return 0
    if source_size is not None and source_size >= 400_000:
        return min(frame_count, 60)
    if source_size is not None and source_size >= 180_000:
        return min(frame_count, 72)
    if frame_count > 120:
        return min(frame_count, 60)
    if frame_count > 90:
        return min(frame_count, 72)
    return min(frame_count, 90)


def _sample_rendered_frames(rendered_frames, max_frames):
    if max_frames <= 0 or len(rendered_frames) <= max_frames:
        return rendered_frames

    total_duration = sum(d for _, d in rendered_frames)
    target_step = total_duration / max_frames

    sampled = []
    acc = 0
    current_target = target_step

    for frame in rendered_frames:
        acc += frame[1]
        if acc >= current_target:
            sampled.append(frame)
            current_target += target_step

    if len(sampled) < max_frames:
        sampled.append(rendered_frames[-1])

    return sampled[:max_frames]


def _terminate_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass

    deadline = time.time() + 1.0
    while time.time() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.05)

    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _run_subprocess(
    cmd: list[str],
    cancel_event=None,
    *,
    capture_output: bool = False,
    text: bool = False,
) -> tuple[str | bytes | None, str | bytes | None]:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture_output else subprocess.DEVNULL,
        text=text,
        start_new_session=True,
    )
    try:
        if capture_output:
            while True:
                _check_cancel(cancel_event)
                try:
                    stdout, stderr = proc.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        else:
            while True:
                _check_cancel(cancel_event)
                try:
                    proc.wait(timeout=0.2)
                    stdout = None
                    stderr = None
                    break
                except subprocess.TimeoutExpired:
                    continue

        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd, output=stdout, stderr=stderr)

        return stdout, stderr
    except BaseException:
        _terminate_process(proc)
        raise


def _probe_webp(webp_path: Path, cancel_event=None) -> tuple[int, int, list[FrameMeta]]:
    stdout, _ = _run_subprocess(
        ["webpmux", "-info", str(webp_path)],
        cancel_event=cancel_event,
        capture_output=True,
        text=True,
    )

    canvas_w = 0
    canvas_h = 0
    frames: list[FrameMeta] = []

    for line in str(stdout).splitlines():
        canvas_match = _CANVAS_RE.search(line)
        if canvas_match:
            canvas_w = int(canvas_match.group(1))
            canvas_h = int(canvas_match.group(2))
            continue

        frame_match = _FRAME_RE.match(line)
        if not frame_match:
            continue

        frames.append(
            FrameMeta(
                index=int(frame_match.group(1)),
                width=int(frame_match.group(2)),
                height=int(frame_match.group(3)),
                x_offset=int(frame_match.group(5)),
                y_offset=int(frame_match.group(6)),
                duration_ms=max(1, int(frame_match.group(7))),
                dispose=frame_match.group(8),
                blend=frame_match.group(9) == "yes",
            )
        )

    if canvas_w <= 0 or canvas_h <= 0 or not frames:
        raise ValueError("Не удалось прочитать данные animated WebP.")

    return canvas_w, canvas_h, frames


def _extract_webp_frame(webp_path: Path, frame_index: int, out_path: Path, cancel_event=None) -> None:
    _run_subprocess(
        [
            "webpmux",
            "-get",
            "frame",
            str(frame_index),
            str(webp_path),
            "-o",
            str(out_path),
        ],
        cancel_event=cancel_event,
    )


def _render_webp_to_png_sequence(
    webp_path: Path,
    frame_dir: Path,
    cancel_event=None,
    max_duration_ms: int = 2950,
    source_size: int | None = None,
) -> tuple[Path, int, int]:

    _check_cancel(cancel_event)

    # 👉 вытаскиваем ВСЕ кадры через ffmpeg
    cmd = [
        "ffmpeg",
        "-y",
        "-c:v", "libwebp",
        "-i", str(webp_path),
        "-vsync", "0",
        str(frame_dir / "frame_%03d.png"),
    ]

    _run_subprocess(cmd, cancel_event=cancel_event)

    frames = sorted(frame_dir.glob("frame_*.png"))
    frame_count = len(frames)

    if frame_count == 0:
        return frame_dir, 0, 0

    # 👉 нормальная длительность (без webpmux)
    total_duration_ms = frame_count * 33  # ~20 FPS

    # 👉 лимит кадров (как было)
    frame_limit = _frame_render_limit(source_size, frame_count)

    if frame_count > frame_limit:
        step = frame_count / frame_limit
        sampled_dir = Path(tempfile.mkdtemp(dir=frame_dir))

        for i in range(frame_limit):
            idx = int(i * step)
            shutil.copy2(frames[idx], sampled_dir / f"frame_{i:03d}.png")

        return sampled_dir, int(frame_limit * 50), frame_limit

    return frame_dir, total_duration_ms, frame_count



def _encode_png_sequence_to_webm(
    frame_dir: Path,
    out_path: Path,
    crf: int,
    total_duration_ms: int,
    frame_count: int,
    target_size: int,
    cancel_event=None,
    cpu_used: int = 4,
) -> None:
    fps = min(
        30,
        max(1.0, frame_count * 1000.0 / max(1, total_duration_ms))
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-threads",
        "1",
        "-framerate",
        f"{fps:.6f}",
        "-i",
        str(frame_dir / "frame_%03d.png"),
        "-vf",
        _scale_filter(target_size),
        "-r",
        f"{fps:.6f}",
        "-an",
        "-c:v",
        "libvpx-vp9",
        "-cpu-used",
        str(cpu_used),
        "-row-mt",
        "1",
        "-pix_fmt",
        "yuva420p",
        "-auto-alt-ref",
        "0",
        "-b:v",
        "0",
        "-crf",
        str(crf),
        str(out_path),
    ]
    _run_subprocess(cmd, cancel_event=cancel_event)


def _render_webp_to_gif(webp_path: Path, gif_path: Path, cancel_event=None) -> None:
    cmd = _im_cmd() + [
        str(webp_path),
        "-coalesce",
        str(gif_path),
    ]
    _run_subprocess(cmd, cancel_event=cancel_event)


def _encode_gif_to_webm(
    gif_path: Path,
    out_path: Path,
    crf: int,
    target_size: int,
    cancel_event=None,
    cpu_used: int = 4,
) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(gif_path),

        "-vf",
        "fps=15:round=down," + _scale_filter(target_size, flags="bilinear"),

        "-an",
        "-c:v", "libvpx-vp9",
        "-pix_fmt", "yuva420p",
        "-auto-alt-ref", "0",
        "-b:v", "0",
        "-crf", str(crf),

        "-cpu-used", str(cpu_used),
        "-row-mt", "1",

        str(out_path),
    ]

    _run_subprocess(cmd, cancel_event=cancel_event)



def _convert_single_webp_main(
    webp_path: Path,
    out_path: Path,
    cancel_event=None,
    source_size: int | None = None,
) -> tuple[bool, str | None]:
    print("PNG PIPELINE ENTER")
    last_reason = None
    target_sizes, crf_start, crf_step, cpu_used = _select_profile(source_size)

    for target_size in target_sizes:
        _check_cancel(cancel_event)
        crf = crf_start

        if out_path.exists():
            try:
                out_path.unlink()
            except Exception:
                pass

        try:
            with tempfile.TemporaryDirectory(dir=webp_path.parent) as tmp:
                frame_dir = Path(tmp)
                encode_dir, total_duration_ms, frame_count = _render_webp_to_png_sequence(
                    webp_path,
                    frame_dir,
                    cancel_event=cancel_event,
                    max_duration_ms=10000,  # 🔥 ВАЖНО
                    source_size=source_size,
                )
                if frame_count <= 0 or total_duration_ms <= 0:
                    return False, "не удалось извлечь кадры"

                attempts = 0
                max_attempts = 2

                while attempts < max_attempts:
                    _check_cancel(cancel_event)

                    if out_path.exists():
                        try:
                            out_path.unlink()
                        except Exception:
                            pass

                    try:
                        _encode_png_sequence_to_webm(
                            encode_dir,
                            out_path,
                            crf,
                            total_duration_ms,
                            frame_count,
                            target_size,
                            cancel_event=cancel_event,
                            cpu_used=cpu_used,
                        )
                    except subprocess.TimeoutExpired:
                        last_reason = f"таймаут на размере {target_size}px, CRF {crf}"
                        attempts += 1
                        crf += crf_step
                        continue

                    except subprocess.CalledProcessError:
                        last_reason = f"ошибка кодирования на размере {target_size}px, CRF {crf}"
                        attempts += 1
                        crf += crf_step
                        continue

                    if out_path.exists() and out_path.stat().st_size <= MAX_WEBM_SIZE:
                        return True, None

                    if out_path.exists():
                        try:
                            out_path.unlink()
                        except Exception:
                            pass

                    last_reason = f"файл больше лимита 64 KB (размер {target_size}px, CRF {crf})"
                    attempts += 1
                    crf += crf_step

        except ConversionCancelled:
            raise
        except subprocess.TimeoutExpired:
            last_reason = f"таймаут на размере {target_size}px, CRF {crf}"
            continue
        except subprocess.CalledProcessError:
            last_reason = f"ошибка кодирования на размере {target_size}px, CRF {crf}"
            continue
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            message = str(exc)
            last_reason = message
            if "animated WebP" in message:
                return False, message
            continue

    hard_ok, hard_reason = _convert_single_webp_hard_fallback(webp_path, out_path, cancel_event=cancel_event, source_size=source_size)
    if hard_ok:
        return True, None

    return False, hard_reason or last_reason or "не удалось уложить WEBM в лимит"



def _convert_single_webp_via_gif(
    webp_path: Path,
    out_path: Path,
    cancel_event=None,
    source_size: int | None = None,
) -> tuple[bool, str | None]:
    print("GIF PIPELINE ENTER")
    last_reason = None
    target_sizes, crf_start, crf_step, cpu_used = _select_profile(source_size)

    for target_size in target_sizes:
        _check_cancel(cancel_event)
        crf = crf_start

        if out_path.exists():
            try:
                out_path.unlink()
            except Exception:
                pass

        try:
            with tempfile.TemporaryDirectory(dir=webp_path.parent) as tmp:
                tmp_dir = Path(tmp)
                gif_path = tmp_dir / f"{webp_path.stem}.gif"
                _render_webp_to_gif(webp_path, gif_path, cancel_event=cancel_event)

                attempts = 0
                max_attempts = 2

                while attempts < max_attempts:
                    _check_cancel(cancel_event)

                    if out_path.exists():
                        try:
                            out_path.unlink()
                        except Exception:
                            pass

                    try:
                        _encode_gif_to_webm(gif_path, out_path, crf, target_size, cancel_event=cancel_event, cpu_used=cpu_used)
                    except subprocess.TimeoutExpired:
                        last_reason = f"таймаут GIF fallback на размере {target_size}px, CRF {crf}"
                        attempts += 1
                        crf += crf_step
                        continue

                    except subprocess.CalledProcessError:
                        last_reason = f"ошибка GIF fallback на размере {target_size}px, CRF {crf}"
                        attempts += 1
                        crf += crf_step
                        continue

                    if out_path.exists() and out_path.stat().st_size <= MAX_WEBM_SIZE:
                        return True, None

                    if out_path.exists():
                        try:
                            out_path.unlink()
                        except Exception:
                            pass

                    last_reason = f"GIF fallback: файл больше лимита 64 KB (размер {target_size}px, CRF {crf})"
                    attempts += 1
                    crf += crf_step

        except ConversionCancelled:
            raise
        except subprocess.TimeoutExpired:
            last_reason = f"таймаут GIF fallback на размере {target_size}px, CRF {crf}"
            continue
        except subprocess.CalledProcessError:
            last_reason = f"ошибка GIF fallback на размере {target_size}px, CRF {crf}"
            continue
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            last_reason = str(exc)
            continue

    # последний шанс — очень агрессивный режим (fps=12)
    try:
        if out_path.exists():
            out_path.unlink()

        cmd = [
            "ffmpeg",
            "-y",
            "-i", str(gif_path),
            "-vf",
            "fps=12," + _scale_filter(target_size, flags="bilinear"),
            "-an",
            "-c:v", "libvpx-vp9",
            "-pix_fmt", "yuva420p",
            "-auto-alt-ref", "0",
            "-b:v", "0",
            "-crf", "52",
            "-cpu-used", "6",
            "-row-mt", "1",
            str(out_path),
        ]

        _run_subprocess(cmd, cancel_event=cancel_event)

        if out_path.exists() and out_path.stat().st_size <= MAX_WEBM_SIZE:
            return True, None

        if out_path.exists():
            out_path.unlink()

    except Exception:
        pass

    hard_ok, hard_reason = _convert_single_webp_hard_fallback(
        webp_path,
        out_path,
        cancel_event=cancel_event,
        source_size=source_size,
    )
    if hard_ok:
        return True, None

    return False, hard_reason or last_reason or "GIF fallback не смог уложить WEBM в лимит"



def _convert_single_webp_hard_fallback(
    webp_path: Path,
    out_path: Path,
    cancel_event=None,
    source_size: int | None = None,
) -> tuple[bool, str | None]:
    last_reason = None
    hard_sizes = (100, 96, 92)
    hard_crf_start = 52
    hard_crf_step = 4
    hard_cpu_used = 6

    for target_size in hard_sizes:
        _check_cancel(cancel_event)
        crf = hard_crf_start

        if out_path.exists():
            try:
                out_path.unlink()
            except Exception:
                pass

        try:
            with tempfile.TemporaryDirectory(dir=webp_path.parent) as tmp:
                frame_dir = Path(tmp)
                encode_dir, total_duration_ms, frame_count = _render_webp_to_png_sequence(
                    webp_path,
                    frame_dir,
                    cancel_event=cancel_event,
                    max_duration_ms=2000,
                    source_size=source_size,
                )
                if frame_count <= 0 or total_duration_ms <= 0:
                    return False, "не удалось извлечь кадры"

                while crf <= 60:
                    _check_cancel(cancel_event)

                    if out_path.exists():
                        try:
                            out_path.unlink()
                        except Exception:
                            pass

                    try:
                        _encode_png_sequence_to_webm(
                            encode_dir,
                            out_path,
                            crf,
                            total_duration_ms,
                            frame_count,
                            target_size,
                            cancel_event=cancel_event,
                            cpu_used=hard_cpu_used,
                        )
                    except subprocess.TimeoutExpired:
                        last_reason = f"таймаут hard fallback на размере {target_size}px, CRF {crf}"
                        crf += hard_crf_step
                        continue
                    except subprocess.CalledProcessError:
                        last_reason = f"ошибка hard fallback на размере {target_size}px, CRF {crf}"
                        crf += hard_crf_step
                        continue

                    if out_path.exists() and out_path.stat().st_size <= MAX_WEBM_SIZE:
                        return True, None

                    if out_path.exists():
                        try:
                            out_path.unlink()
                        except Exception:
                            pass

                    last_reason = f"hard fallback: файл больше лимита 64 KB (размер {target_size}px, CRF {crf})"
                    crf += hard_crf_step

        except ConversionCancelled:
            raise
        except Exception as exc:
            last_reason = str(exc)
            continue

    return False, last_reason or "hard fallback не смог уложить WEBM в лимит"

def _convert_single_webp(
    webp_path: Path,
    out_path: Path,
    cancel_event=None,
    source_size: int | None = None,
) -> tuple[bool, str | None]:

    ok, reason = _convert_single_webp_main(
        webp_path, out_path, cancel_event=cancel_event, source_size=source_size
    )
    if ok:
        return True, None

    # 👉 ВАЖНО: сначала hard fallback (PNG с ограничением по времени)
    hard_ok, hard_reason = _convert_single_webp_hard_fallback(
        webp_path, out_path, cancel_event=cancel_event, source_size=source_size
    )
    if hard_ok:
        return True, None

    # 👉 И ТОЛЬКО ПОТОМ GIF
    gif_ok, gif_reason = _convert_single_webp_via_gif(
        webp_path, out_path, cancel_event=cancel_event, source_size=source_size
    )
    if gif_ok:
        return True, None

    return False, gif_reason or hard_reason or reason or "не удалось конвертировать WEBP"

def convert_webp_to_webm(webp_path: Path, out_path: Path, cancel_event=None, source_size: int | None = None) -> tuple[bool, str | None]:
    return _convert_single_webp(webp_path, out_path, cancel_event=cancel_event, source_size=source_size)


async def convert_to_telegram_format(work_dir: Path, status_msg, cancel_event=None, reply_markup=None):
    webm_dir = work_dir / "telegram_emotes"
    webm_dir.mkdir(exist_ok=True)

    async def safe_edit(text: str) -> None:
        if cancel_event is not None and cancel_event.is_set():
            return
        try:
            await status_msg.edit_text(text, reply_markup=reply_markup)
        except Exception:
            pass

    webp_files = sorted(work_dir.glob("*.webp"))
    total = len(webp_files)
    converted = 0
    skipped = 0
    skipped_items: list[tuple[str, str]] = []

    for index, webp in enumerate(webp_files, 1):
        if cancel_event is not None and cancel_event.is_set():
            break

        name = webp.stem
        await safe_edit(
            f"🎬 Конвертация в WEBM...\nВсего: {total}\nКонвертировано: {converted}/{total}\nОшибки: {skipped}\nТекущий: {name}",
        )

        out_path = webm_dir / f"{webp.stem}.webm"
        try:
            source_size = webp.stat().st_size if webp.exists() else None
            ok, reason = await asyncio.to_thread(_convert_single_webp, webp, out_path, cancel_event, source_size)
        except ConversionCancelled:
            break
        except Exception as exc:
            ok = False
            reason = f"неожиданная ошибка: {exc}"

        if ok:
            converted += 1
        else:
            skipped += 1
            skipped_items.append((name, reason or "неизвестная ошибка"))
            await safe_edit(
                f"⚠️ Ошибка: {name}\nПричина: {reason or 'неизвестная ошибка'}\nВсего: {total}\nКонвертировано: {converted}/{total}\nОшибки: {skipped}\nТекущий: {name}",
            )

        if (index % 5 == 0 or index == total) and not (cancel_event is not None and cancel_event.is_set()):
            await safe_edit(
                f"🎬 Конвертация в WEBM...\nВсего: {total}\nКонвертировано: {converted}/{total}\nОшибки: {skipped}\nТекущий: {name}",
            )

    return webm_dir, converted, skipped, skipped_items
