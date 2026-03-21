import asyncio
import shutil
import zipfile

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import CDN_BASE, SAVE_ROOT
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

CANCEL_CALLBACK_DATA = "cancel_job"


def cancel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("ОТМЕНА", callback_data=CANCEL_CALLBACK_DATA)]])


async def _edit_status(status_msg, text: str, active: bool = True):
    try:
        await status_msg.edit_text(text, reply_markup=cancel_markup() if active else None)
    except Exception:
        pass


def _build_zip(webm_dir, zip_path):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(webm_dir.glob("*.webm")):
            z.write(f, f.name)


async def _send_zip_archive(update: Update, webm_dir, zip_path, filename: str) -> bool:
    _build_zip(webm_dir, zip_path)
    if zip_path.exists() and zip_path.stat().st_size > 0:
        with zip_path.open("rb") as archive:
            await update.message.reply_document(archive, filename=filename)
        return True
    return False


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    set_id = extract_set_id(text)
    if set_id:
        await handle_emote_set(update, context, set_id)
        return

    emote_id = extract_emote_id(text)
    if emote_id:
        await handle_single_emote(update, context, emote_id)
        return


async def handle_emote_set(update: Update, context: ContextTypes.DEFAULT_TYPE, set_id: str):
    cancel_event = asyncio.Event()
    context.chat_data["cancel_event"] = cancel_event

    status_msg = await update.message.reply_text("⏳ Подготовка...", reply_markup=cancel_markup())
    work_dir = SAVE_ROOT / set_id
    work_dir.mkdir(exist_ok=True)
    zip_path = SAVE_ROOT / f"{set_id}.zip"

    try:
        data = fetch_emote_list(set_id)
        if not data or "emotes" not in data:
            await _edit_status(status_msg, "Ошибка получения списка эмоутов.", active=False)
            return

        total = len(data["emotes"])
        downloaded = 0
        skipped_downloads = 0

        await _edit_status(
            status_msg,
            f"📥 Скачивание эмоутов...\nГотово: 0/{total}\nПропущено: 0\nТекущий: —",
        )

        for emote in data["emotes"]:
            if cancel_event.is_set():
                break

            emote_data = unwrap_emote(emote)
            if not emote_data:
                skipped_downloads += 1
                continue

            name = safe_name(emote_data.get("name", "unnamed"))
            emote_id = emote_data.get("id")
            files = emote_data.get("host", {}).get("files", [])
            best_file = get_best_file(files)

            if not best_file or not emote_id:
                skipped_downloads += 1
                continue

            await _edit_status(
                status_msg,
                f"📥 Скачивание эмоутов...\nГотово: {downloaded}/{total}\nПропущено: {skipped_downloads}\nТекущий: {name}",
            )

            url = CDN_BASE.format(id=emote_id, file=best_file)
            save_path = work_dir / f"{name}.webp"

            ok = await asyncio.to_thread(download_file, url, save_path)
            if ok:
                downloaded += 1
            else:
                skipped_downloads += 1

        if cancel_event.is_set():
            await _edit_status(status_msg, "⛔ Отмена... Сохраняю то, что уже готово.")

        await _edit_status(
            status_msg,
            f"⚙️ Скачано {downloaded}/{total}\n🎬 Конвертация в WEBM...",
        )

        webm_dir, cancelled, converted, skipped_convert = await convert_to_telegram_format(
            work_dir,
            status_msg,
            cancel_event=cancel_event,
            reply_markup=cancel_markup(),
        )

        if cancelled or cancel_event.is_set():
            if not any(webm_dir.glob("*.webm")):
                await _edit_status(status_msg, "⛔ Отмена. Готовых WEBM нет.", active=False)
                return

            await _edit_status(status_msg, "📦 Архивирую готовые WEBM...")
            sent = await _send_zip_archive(update, webm_dir, zip_path, f"{set_id}.zip")
            if sent:
                await _edit_status(status_msg, "⛔ Отмена выполнена. Частичный архив отправлен.", active=False)
            else:
                await _edit_status(status_msg, "⛔ Отмена. Архив не удалось собрать.", active=False)
            return

        await _edit_status(status_msg, f"📦 Упаковываю архив...\nWEBM: {converted}\nПропущено: {skipped_convert}")
        sent = await _send_zip_archive(update, webm_dir, zip_path, f"{set_id}.zip")
        if not sent:
            await _edit_status(status_msg, "Не удалось собрать итоговый файл.", active=False)
            return

        await _edit_status(status_msg, "✅ Готово!", active=False)
    finally:
        context.chat_data.pop("cancel_event", None)
        try:
            shutil.rmtree(work_dir)
        except Exception:
            pass
        try:
            zip_path.unlink()
        except Exception:
            pass


async def handle_single_emote(update: Update, context: ContextTypes.DEFAULT_TYPE, emote_id: str):
    cancel_event = asyncio.Event()
    context.chat_data["cancel_event"] = cancel_event

    status_msg = await update.message.reply_text("⏳ Подготовка...", reply_markup=cancel_markup())
    work_dir = SAVE_ROOT / emote_id
    work_dir.mkdir(exist_ok=True)
    zip_path = SAVE_ROOT / f"{emote_id}.zip"

    try:
        payload = fetch_emote(emote_id)
        emote = unwrap_emote(payload)
        if not emote:
            await _edit_status(status_msg, "Ошибка получения эмоута.", active=False)
            return

        name = safe_name(emote.get("name", emote_id))
        files = emote.get("host", {}).get("files", [])
        best_file = get_best_file(files)

        if not best_file:
            await _edit_status(status_msg, "У эмоута нет WEBP-файла.", active=False)
            return

        await _edit_status(
            status_msg,
            "📥 Скачивание эмоута...\nГотово: 0/1\nПропущено: 0\nТекущий: 1/1",
        )

        url = CDN_BASE.format(id=emote_id, file=best_file)
        save_path = work_dir / f"{name}.webp"

        if not await asyncio.to_thread(download_file, url, save_path):
            await _edit_status(status_msg, "Не удалось скачать эмоут.", active=False)
            return

        if cancel_event.is_set():
            await _edit_status(status_msg, "⛔ Отмена... Сохраняю то, что уже готово.")

        await _edit_status(
            status_msg,
            "🎬 Конвертация в WEBM...\nГотово: 0/1\nПропущено: 0\nТекущий: 1/1",
        )

        webm_dir, cancelled, converted, skipped_convert = await convert_to_telegram_format(
            work_dir,
            status_msg,
            cancel_event=cancel_event,
            reply_markup=cancel_markup(),
        )
        webm_files = sorted(webm_dir.glob("*.webm"))

        if cancelled or cancel_event.is_set():
            if not webm_files:
                await _edit_status(status_msg, "⛔ Отмена. Готовых WEBM нет.", active=False)
                return

            await _edit_status(status_msg, "📦 Архивирую готовый WEBM...")
            sent = await _send_zip_archive(update, webm_dir, zip_path, f"{name}.zip")
            if sent:
                await _edit_status(status_msg, "⛔ Отмена выполнена. Частичный архив отправлен.", active=False)
            else:
                await _edit_status(status_msg, "⛔ Отмена. Архив не удалось собрать.", active=False)
            return

        if not webm_files:
            await _edit_status(status_msg, "Не удалось собрать итоговый файл.", active=False)
            return

        result_file = webm_files[0]
        with result_file.open("rb") as f:
            await update.message.reply_document(f, filename=result_file.name)

        await _edit_status(status_msg, "✅ Готово!", active=False)
    finally:
        context.chat_data.pop("cancel_event", None)
        try:
            shutil.rmtree(work_dir)
        except Exception:
            pass
        try:
            zip_path.unlink()
        except Exception:
            pass


async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None:
        return

    await query.answer("Отмена запрошена")
    cancel_event = context.chat_data.get("cancel_event")
    if cancel_event is not None:
        cancel_event.set()

    try:
        await query.message.edit_text("⛔ Отмена запрошена...")
    except Exception:
        pass


def about_text() -> str:
    return "Бот запущен."


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(about_text())
