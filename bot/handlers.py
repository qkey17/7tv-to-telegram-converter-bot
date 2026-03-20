import zipfile
import shutil
from telegram import Update
from telegram.ext import ContextTypes

from config import SAVE_ROOT, CDN_BASE
from utils.filenames import safe_name
from seven_tv.api import extract_set_id, fetch_emote_list, get_best_file
from downloader.downloader import download_file
from converter.converter import convert_to_telegram_format


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    set_id = extract_set_id(text)

    if not set_id:
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

    try:
        shutil.rmtree(work_dir)
        zip_path.unlink()
    except Exception as e:
        print(f"Ошибка очистки: {e}")


def about_text() -> str:
    return "Бот запущен."


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(about_text())