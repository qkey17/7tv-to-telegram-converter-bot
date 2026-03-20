import subprocess
from pathlib import Path

async def convert_to_telegram_format(work_dir: Path, status_msg):
    gifs_dir = work_dir / "gifs"
    webm_dir = work_dir / "telegram_emotes"
    gifs_dir.mkdir(exist_ok=True)
    webm_dir.mkdir(exist_ok=True)

    webp_files = list(work_dir.glob("*.webp"))
    total = len(webp_files)
    done = 0

    for webp in webp_files:
        gif_path = gifs_dir / (webp.stem + ".gif")
        subprocess.run([
            "convert", str(webp),
            "-coalesce",
            "-resize", "100x100",
            str(gif_path)
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        done += 1
        if done % 5 == 0 or done == total:
            await status_msg.edit_text(f"🎞 Конвертация в GIF: {done}/{total}")

    await status_msg.edit_text("🎬 Кодирование в WEBM...")

    def encode_gif(gif_path: Path, crf: int):
        out = webm_dir / (gif_path.stem + ".webm")
        subprocess.run([
            "ffmpeg", "-y", "-i", str(gif_path),
            "-vf", "fps=30,scale=100:100:flags=lanczos:force_original_aspect_ratio=decrease,"
                   "pad=100:100:(ow-iw)/2:(oh-ih)/2:color=0x00000000",
            "-frames:v", "90",
            "-an",
            "-c:v", "libvpx-vp9",
            "-pix_fmt", "yuva420p",
            "-auto-alt-ref", "0",
            "-b:v", "0",
            "-crf", str(crf),
            str(out)
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    gif_files = list(gifs_dir.glob("*.gif"))
    for gif in gif_files:
        encode_gif(gif, 32)

    await status_msg.edit_text("🗜 Проверка размера...")

    for webm in webm_dir.glob("*.webm"):
        if webm.stat().st_size > 64 * 1024:
            webm.unlink()

    await status_msg.edit_text("🗜 Оптимизация размера...")

    crf = 36
    while True:
        missing = []
        for gif in gifs_dir.glob("*.gif"):
            webm = webm_dir / (gif.stem + ".webm")
            if not webm.exists():
                missing.append(gif)

        if not missing or crf > 50:
            break

        for gif in missing:
            encode_gif(gif, crf)

        for webm in webm_dir.glob("*.webm"):
            if webm.stat().st_size > 64 * 1024:
                webm.unlink()

        crf += 2

    return webm_dir