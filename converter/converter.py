import asyncio
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

try:
    import webp
except ModuleNotFoundError as exc:
    raise ModuleNotFoundError(
        "Не найден модуль webp. Установи его в venv: /opt/7tv-to-telegram-converter-bot/venv/bin/pip install webp"
    ) from exc

MAX_WEBM_SIZE = 64 * 1024
MAX_OUTPUT_DURATION_MS = 2950
TARGET_SIZES = (100, 96, 92, 88, 84)
CRF_START = 32
CRF_STEP = 2
CRF_MAX = 60


def _escape_concat_path(path: Path) -> str:
    return str(path).replace("'", r"'\''")


def _render_webp_to_png_sequence(webp_path: Path, frame_dir: Path) -> list[int]:
    with webp_path.open("rb") as f:
        webp_data = webp.WebPData.from_buffer(f.read())
        decoder = webp.WebPAnimDecoder.new(webp_data)

        durations: list[int] = []
        previous_timestamp = 0
        frame_index = 0

        for arr, timestamp_ms in decoder.frames():
            frame_index += 1
            img = Image.fromarray(arr, "RGBA")
            img.save(frame_dir / f"frame_{frame_index:03d}.png", format="PNG")

            timestamp_ms = int(timestamp_ms)
            duration_ms = max(1, timestamp_ms - previous_timestamp)
            durations.append(duration_ms)
            previous_timestamp = timestamp_ms

    if not durations:
        raise ValueError("Не удалось прочитать данные animated WebP.")

    return durations


def _write_concat_file(frame_dir: Path, durations: list[int], concat_path: Path) -> None:
    with concat_path.open("w", encoding="utf-8") as f:
        for index, duration_ms in enumerate(durations, 1):
            frame_path = _escape_concat_path(frame_dir / f"frame_{index:03d}.png")
            f.write(f"file '{frame_path}'\n")
            f.write(f"duration {duration_ms / 1000:.6f}\n")

        last_frame = _escape_concat_path(frame_dir / f"frame_{len(durations):03d}.png")
        f.write(f"file '{last_frame}'\n")


def _encode_png_sequence_to_webm(concat_path: Path, out_path: Path, crf: int, target_size: int) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_path),
        "-t",
        str(MAX_OUTPUT_DURATION_MS / 1000),
        "-vf",
        (
            f"scale={target_size}:{target_size}:flags=lanczos:force_original_aspect_ratio=decrease,"
            f"pad={target_size}:{target_size}:(ow-iw)/2:(oh-ih)/2:color=0x00000000"
        ),
        "-an",
        "-vsync",
        "vfr",
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
                    concat_path = frame_dir / "frames.txt"
                    _write_concat_file(frame_dir, durations, concat_path)
                    _encode_png_sequence_to_webm(concat_path, out_path, crf, target_size)
            except subprocess.TimeoutExpired:
                last_reason = f"таймаут на размере {target_size}px, CRF {crf}"
                crf += CRF_STEP
                continue
            except subprocess.CalledProcessError:
                last_reason = f"ошибка кодирования на размере {target_size}px, CRF {crf}"
                crf += CRF_STEP
                continue
            except (OSError, ValueError) as exc:
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

    for index, webp_file in enumerate(webp_files, 1):
        if cancel_event is not None and cancel_event.is_set():
            break

        name = webp_file.stem
        await status_msg.edit_text(
            f"🎬 Конвертация в WEBM...\nГотово: {converted}/{total}\nПропущено: {skipped}\nТекущий: {name}",
            reply_markup=reply_markup,
        )

        out_path = webm_dir / f"{webp_file.stem}.webm"
        try:
            ok, reason = await asyncio.to_thread(_convert_single_webp, webp_file, out_path)
        except Exception as exc:
            ok = False
            reason = f"неожиданная ошибка: {exc}"

        if ok:
            converted += 1
        else:
            skipped += 1
            skipped_items.append((name, reason or "неизвестная ошибка"))
            await status_msg.edit_text(
                f"⚠️ Пропущен: {name}\nПричина: {reason or 'неизвестная ошибка'}\nГотово: {converted}/{total}\nПропущено: {skipped}\nТекущий: {name}",
                reply_markup=reply_markup,
            )

        if index % 5 == 0 or index == total:
            await status_msg.edit_text(
                f"🎬 Конвертация в WEBM...\nГотово: {converted}/{total}\nПропущено: {skipped}\nТекущий: {name}",
                reply_markup=reply_markup,
            )

    return webm_dir, converted, skipped, skipped_items
