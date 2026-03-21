import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageSequence

MAX_WEBM_SIZE = 64 * 1024
TARGET_FPS = 30
TARGET_SIZE = 100
CRF_START = 32
CRF_STEP = 2
CRF_MAX = 50


def _render_webp_to_png_sequence(webp_path: Path, frame_dir: Path) -> None:
    with Image.open(webp_path) as im:
        for index, frame in enumerate(ImageSequence.Iterator(im), 1):
            frame_path = frame_dir / f"frame_{index:03d}.png"
            frame.convert("RGBA").save(frame_path, format="PNG")


def _encode_png_sequence_to_webm(frame_dir: Path, out_path: Path, crf: int) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-framerate", str(TARGET_FPS),
        "-i", str(frame_dir / "frame_%03d.png"),
        "-vf", (
            f"fps={TARGET_FPS},"
            f"scale={TARGET_SIZE}:{TARGET_SIZE}:flags=lanczos:force_original_aspect_ratio=decrease,"
            f"pad={TARGET_SIZE}:{TARGET_SIZE}:(ow-iw)/2:(oh-ih)/2:color=0x00000000"
        ),
        "-frames:v", "90",
        "-an",
        "-c:v", "libvpx-vp9",
        "-pix_fmt", "yuva420p",
        "-auto-alt-ref", "0",
        "-b:v", "0",
        "-crf", str(crf),
        str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _convert_single_webp(webp_path: Path, out_path: Path) -> bool:
    crf = CRF_START

    while crf <= CRF_MAX:
        if out_path.exists():
            try:
                out_path.unlink()
            except Exception:
                pass

        with tempfile.TemporaryDirectory(dir=webp_path.parent) as tmp:
            frame_dir = Path(tmp)
            _render_webp_to_png_sequence(webp_path, frame_dir)
            _encode_png_sequence_to_webm(frame_dir, out_path, crf)

        if out_path.exists() and out_path.stat().st_size <= MAX_WEBM_SIZE:
            return True

        if out_path.exists():
            try:
                out_path.unlink()
            except Exception:
                pass

        crf += CRF_STEP

    return False


async def convert_to_telegram_format(work_dir: Path, status_msg):
    webm_dir = work_dir / "telegram_emotes"
    webm_dir.mkdir(exist_ok=True)

    webp_files = sorted(work_dir.glob("*.webp"))
    total = len(webp_files)

    for index, webp in enumerate(webp_files, 1):
        out_path = webm_dir / f"{webp.stem}.webm"
        try:
            _convert_single_webp(webp, out_path)
        except subprocess.CalledProcessError:
            if out_path.exists():
                try:
                    out_path.unlink()
                except Exception:
                    pass
            continue

        if index % 5 == 0 or index == total:
            await status_msg.edit_text(f"🎬 Кодирование в WEBM: {index}/{total}")

    return webm_dir
