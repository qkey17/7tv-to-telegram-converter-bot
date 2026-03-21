import subprocess
from pathlib import Path


def encode_webp(webp_path: Path, out_path: Path, crf: int):
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(webp_path),
        "-vf", (
            "fps=30,"
            "scale=100:100:flags=lanczos:force_original_aspect_ratio=decrease,"
            "pad=100:100:(ow-iw)/2:(oh-ih)/2:color=0x00000000"
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


async def convert_to_telegram_format(work_dir: Path, status_msg):
    webm_dir = work_dir / "telegram_emotes"
    webm_dir.mkdir(exist_ok=True)

    webp_files = list(work_dir.glob("*.webp"))
    total = len(webp_files)
    done = 0

    crf = 32
    while True:
        for webp in webp_files:
            out_path = webm_dir / (webp.stem + ".webm")
            if out_path.exists():
                continue
            try:
                encode_webp(webp, out_path, crf)
            except subprocess.CalledProcessError:
                if out_path.exists():
                    try:
                        out_path.unlink()
                    except Exception:
                        pass
                continue

            if out_path.stat().st_size > 64 * 1024:
                try:
                    out_path.unlink()
                except Exception:
                    pass

            done += 1
            if done % 5 == 0 or done == total:
                await status_msg.edit_text(f"🎬 Кодирование в WEBM: {done}/{total}")

        missing = [
            webp for webp in webp_files
            if not (webm_dir / (webp.stem + ".webm")).exists()
        ]

        if not missing or crf > 50:
            break

        crf += 2

    return webm_dir
