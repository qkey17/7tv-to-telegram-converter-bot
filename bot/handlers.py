import shutil
import zipfile

from telegram import Update
from telegram.ext import ContextTypes

from config import SAVE_ROOT, CDN_BASE
from converter.converter import convert_to_telegram_format
from downloader.downloader import download_file
from seven_tv.api import (
    extract_emote_id,
    extract_set_id,
    fetch_emote,
    fetch_emote_list,
    get_best_file,
    unwrap_emote,
)
from utils.filenames import safe_name


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    set_id = extract_set_id(text)
    if set_id:
        await handle_emote_set(update, set_id)
        return

    emote_id = extract_emote_id(text)
    if emote_id:
        await handle_single_emote(update, emote_id)
        return


async def handle_emote_set(update: Update, set_id: str):
    status_msg = await update.message.reply_text("⏳ Подготовка...")
    work_dir = SAVE_ROOT / set_id
    work_dir.mkdir(exist_ok=True)
    zip_path = SAVE_ROOT / f"{set_id}.zip"

    try:
        data = fetch_emote_list(set_id)
        if not data or "emotes" not in data:
            await update.message.reply_text("Ошибка получения списка эмоутов.")
            return

        total = len(data["emotes"])
        done = 0

        for emote in data["emotes"]:
            emote_data = unwrap_emote(emote)
            if not emote_data:
                continue

            name = safe_name(emote_data.get("name", "unnamed"))
            emote_id = emote_data.get("id")
            files = emote_data.get("host", {}).get("files", [])
            best_file = get_best_file(files)

            if not best_file or not emote_id:
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
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for f in webm_dir.iterdir():
                z.write(f, f.name)

        with zip_path.open("rb") as archive:
            await update.message.reply_document(archive)

        await status_msg.edit_text("✅ Готово!")
    finally:
        try:
            shutil.rmtree(work_dir)
        except Exception:
            pass
        try:
            zip_path.unlink()
        except Exception:
            pass


async def handle_single_emote(update: Update, emote_id: str):
    status_msg = await update.message.reply_text("⏳ Подготовка...")
    work_dir = SAVE_ROOT / emote_id
    work_dir.mkdir(exist_ok=True)

    try:
        payload = fetch_emote(emote_id)
        emote = unwrap_emote(payload)
        if not emote:
            await update.message.reply_text("Ошибка получения эмоута.")
            return

        name = safe_name(emote.get("name", emote_id))
        files = emote.get("host", {}).get("files", [])
        best_file = get_best_file(files)

        if not best_file:
            await update.message.reply_text("У эмоута нет WEBP-файла.")
            return

        await status_msg.edit_text("📥 Скачивание эмоута...")

        url = CDN_BASE.format(id=emote_id, file=best_file)
        save_path = work_dir / f"{name}.webp"

        if not download_file(url, save_path):
            await update.message.reply_text("Не удалось скачать эмоут.")
            return

        await status_msg.edit_text("🎞 Конвертация...")

        webm_dir = await convert_to_telegram_format(work_dir, status_msg)
        webm_files = sorted(webm_dir.glob("*.webm"))

        if not webm_files:
            await update.message.reply_text("Не удалось собрать итоговый файл.")
            return

        result_file = webm_files[0]
        with result_file.open("rb") as f:
            await update.message.reply_document(f, filename=result_file.name)

        await status_msg.edit_text("✅ Готово!")
    finally:
        try:
            shutil.rmtree(work_dir)
        except Exception:
            pass


def about_text() -> str:
    return "Бот запущен."


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(about_text())
