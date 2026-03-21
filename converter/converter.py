import asyncio
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

MAX_WEBM_SIZE = 64 * 1024
TARGET_FRAME_DURATION_MS = 30
TARGET_SIZES = (100, 96, 92, 88, 84)
CRF_START = 32
CRF_STEP = 2
CRF_MAX = 60


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


def _probe_webp(webp_path: Path) -> tuple[int, int, list[FrameMeta]]:
    result = subprocess.run(
        ["webpmux", "-info", str(webp_path)],
        check=True,
        capture_output=True,
        text=True,
    )

    canvas_w = 0
    canvas_h = 0
    frames: list[FrameMeta] = []

    for line in result.stdout.splitlines():
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


def _extract_webp_frame(webp_path: Path, frame_index: int, out_path: Path) -> None:
    subprocess.run(
        [
            "webpmux",
            "-get",
            "frame",
            str(frame_index),
            str(webp_path),
            "-o",
            str(out_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _render_webp_to_png_sequence(webp_path: Path, frame_dir: Path) -> list[int]:
    canvas_w, canvas_h, frames = _probe_webp(webp_path)
    durations: list[int] = []

    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

    with tempfile.TemporaryDirectory(dir=frame_dir) as extract_tmp:
        extract_dir = Path(extract_tmp)

        for meta in frames:
            patch_path = extract_dir / f"frame_{meta.index:03d}.webp"
            _extract_webp_frame(webp_path, meta.index, patch_path)

            with Image.open(patch_path) as patch_image:
                patch = patch_image.convert("RGBA")

            current = canvas.copy()
            clear_box = (
                meta.x_offset,
                meta.y_offset,
                meta.x_offset + patch.width,
                meta.y_offset + patch.height,
            )

            if not meta.blend:
                current.paste((0, 0, 0, 0), clear_box)

            current.paste(patch, (meta.x_offset, meta.y_offset), patch)

            output_path = frame_dir / f"frame_{meta.index:03d}.png"
            current.save(output_path, format="PNG")

            durations.append(meta.duration_ms)
            canvas = current

            if meta.dispose == "background":
                canvas.paste((0, 0, 0, 0), clear_box)

    return durations


def _encode_png_sequence_to_webm(
    frame_dir: Path,
    out_path: Path,
    crf: int,
    frame_duration_ms: int,
    target_size: int,
) -> None:
    fps = 1000 / frame_duration_ms

    cmd = [
        "ffmpeg",
        "-y",
        "-framerate",
        f"{fps:.6f}",
        "-i",
        str(frame_dir / "frame_%03d.png"),
        "-t",
        "2.95",
        "-vf",
        (
            f"scale={target_size}:{target_size}:flags=lanczos:force_original_aspect_ratio=decrease,"
            f"pad={target_size}:{target_size}:(ow-iw)/2:(oh-ih)/2:color=0x00000000"
        ),
        "-an",
        "-c:v",
        "libvpx-vp9",
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
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _convert_single_webp(webp_path: Path, out_path: Path) -> tuple[bool, str | None]:
    last_reason = None

    for target_size in TARGET_SIZES:
        crf = CRF_START

        while crf <= CRF_MAX:
            if out_path.exists():
                try:
                    out_path.unlink()
                except Exception:
                    pass

            try:
                with tempfile.TemporaryDirectory(dir=webp_path.parent) as tmp:
                    frame_dir = Path(tmp)
                    durations = _render_webp_to_png_sequence(webp_path, frame_dir)
                    if not durations:
                        return False, "не удалось извлечь кадры"

                    frame_duration_ms = durations[0] if durations else TARGET_FRAME_DURATION_MS
                    _encode_png_sequence_to_webm(
                        frame_dir,
                        out_path,
                        crf,
                        frame_duration_ms,
                        target_size,
                    )
            except subprocess.TimeoutExpired:
                last_reason = f"таймаут на размере {target_size}px, CRF {crf}"
                crf += CRF_STEP
                continue
            except subprocess.CalledProcessError:
                last_reason = f"ошибка кодирования на размере {target_size}px, CRF {crf}"
                crf += CRF_STEP
                continue
            except (UnidentifiedImageError, OSError, ValueError) as exc:
                return False, str(exc)

            if out_path.exists() and out_path.stat().st_size <= MAX_WEBM_SIZE:
                return True, None

            if out_path.exists():
                try:
                    out_path.unlink()
                except Exception:
                    pass

            last_reason = (
                f"файл больше лимита 64 KB (размер {target_size}px, CRF {crf})"
            )
            crf += CRF_STEP

    return False, last_reason or "не удалось уложить WEBM в лимит"


async def convert_to_telegram_format(work_dir: Path, status_msg, cancel_event=None, reply_markup=None):
    webm_dir = work_dir / "telegram_emotes"
    webm_dir.mkdir(exist_ok=True)

    webp_files = sorted(work_dir.glob("*.webp"))
    total = len(webp_files)
    converted = 0
    skipped = 0
    skipped_items: list[tuple[str, str]] = []

    for index, webp in enumerate(webp_files, 1):
        if cancel_event is not None and cancel_event.is_set():
            break

        name = webp.stem
        await status_msg.edit_text(
            f"🎬 Конвертация в WEBM...\nГотово: {converted}/{total}\nПропущено: {skipped}\nТекущий: {name}",
            reply_markup=reply_markup,
        )

        out_path = webm_dir / f"{webp.stem}.webm"
        try:
            ok, reason = await asyncio.to_thread(_convert_single_webp, webp, out_path)
        except Exception as exc:
            ok = False
            reason = f"неожиданная ошибка: {exc}"

        if ok:
            converted += 1
        else:
            skipped += 1
            skipped_items.append((name, reason or "неизвестная ошибка"))
            await status_msg.edit_text(
                f"⚠️ Пропущен: {name}\nПричина: {reason or 'неизвестная ошибка'}\n"
                f"Готово: {converted}/{total}\nПропущено: {skipped}\nТекущий: {name}",
                reply_markup=reply_markup,
            )

        if index % 5 == 0 or index == total:
            await status_msg.edit_text(
                f"🎬 Конвертация в WEBM...\nГотово: {converted}/{total}\nПропущено: {skipped}\nТекущий: {name}",
                reply_markup=reply_markup,
            )

    return webm_dir, converted, skipped, skipped_items
