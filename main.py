import os
import re
import requests
import zipfile
import shutil
import subprocess
from pathlib import Path
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SAVE_ROOT = Path("downloads")
SAVE_ROOT.mkdir(exist_ok=True)

API_TEMPLATE = "https://api.7tv.app/v3/emote-sets/{set_id}"
CDN_BASE = "https://cdn.7tv.app/emote/{id}/{file}"

if not BOT_TOKEN:
    raise ValueError("Не найден TELEGRAM_BOT_TOKEN в переменных среды")

def safe_name(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    name = name.strip()
    return name or "unnamed"


def extract_set_id(text: str) -> str | None:
    m = re.search(r"7tv\.app\/emote-sets\/([A-Za-z0-9]+)", text)
    return m.group(1) if m else None


def fetch_emote_list(set_id: str) -> dict | None:
    try:
        r = requests.get(API_TEMPLATE.format(set_id=set_id), timeout=20)
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        return None


def download_file(url: str, path: Path) -> bool:
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        path.write_bytes(r.content)
        return True
    except requests.RequestException:
        return False


def get_best_file(files: list[dict]) -> str | None:
    webp_files = [f for f in files if f.get("format") == "WEBP"]
    if not webp_files:
        return None
    best = max(webp_files, key=lambda x: x.get("size", 0))
    return best.get("name")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    set_id = extract_set_id(text)

    if not set_id:
        await update.message.reply_text("Пришли ссылку на пак 7TV.")
        return

    status_msg = await update.message.reply_text("⏳ Подготовка...")

    work_dir = SAVE_ROOT / set_id
    work_dir.mkdir(exist_ok=True)

    data = fetch_emote_list(set_id)
    if not data or "emotes" not in data:
        await update.message.reply_text("Ошибка получения списка эмоутов.")
        return

    total = len(data["emotes"])
    done = 0

    for emote in data["emotes"]:
        name = safe_name(emote.get("name", "unnamed"))
        emote_id = emote["data"]["id"]
        files = emote["data"].get("host", {}).get("files", [])
        best_file = get_best_file(files)

        if not best_file:
            continue

        url = CDN_BASE.format(id=emote_id, file=best_file)
        save_path = work_dir / f"{name}.webp"

        if download_file(url, save_path):
            done += 1
        if done % 5 == 0 or done == total:
            await status_msg.edit_text(f"📥 Скачивание эмоутов: {done}/{total}")

    await status_msg.edit_text(f"⚙️ Скачано {done}/{total}\n🎞 Конвертация в GIF...")

    webm_dir = await convert_to_telegram_format(work_dir, status_msg)

    await status_msg.edit_text("📦 Упаковываю архив...")

    zip_path = SAVE_ROOT / f"{set_id}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in webm_dir.iterdir():
            z.write(f, f.name)

    await update.message.reply_document(zip_path.open("rb"))
    await status_msg.edit_text("✅ Готово!")

    # очистка
    try:
        shutil.rmtree(work_dir)  # папка с эмоутами
        zip_path.unlink()  # архив
    except Exception as e:
        print(f"Ошибка очистки: {e}")


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot started")
    app.run_polling(drop_pending_updates=True)


async def convert_to_telegram_format(work_dir: Path, status_msg):
    gifs_dir = work_dir / "gifs"
    webm_dir = work_dir / "telegram_emotes"
    gifs_dir.mkdir(exist_ok=True)
    webm_dir.mkdir(exist_ok=True)

    # ---------- WEBP → GIF ----------
    webp_files = list(work_dir.glob("*.webp"))
    total = len(webp_files)
    done = 0

    for webp in webp_files:
        gif_path = gifs_dir / (webp.stem + ".gif")
        subprocess.run([
            "magick", str(webp),
            "-coalesce",
            "-resize", "100x100",
            str(gif_path)
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        done += 1
        if done % 5 == 0 or done == total:
            await status_msg.edit_text(f"🎞 Конвертация в GIF: {done}/{total}")

    # ---------- GIF → WEBM ----------
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

    # ---------- Удаляем тяжёлые ----------
    await status_msg.edit_text("🗜 Проверка размера...")

    for webm in webm_dir.glob("*.webm"):
        if webm.stat().st_size > 64 * 1024:
            webm.unlink()

    # ---------- Дожатие ----------
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


if __name__ == "__main__":
    main()