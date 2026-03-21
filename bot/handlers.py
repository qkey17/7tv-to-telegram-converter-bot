from __future__ import annotations

import asyncio
import shutil
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import CDN_BASE, SAVE_ROOT
from converter.converter import convert_to_telegram_format, convert_webp_to_webm
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
FINAL_SUMMARY_LIMIT = 20


@dataclass
class JobState:
    kind: str
    cancel_event: threading.Event
    status_msg: Any
    task: asyncio.Task | None = None


_ACTIVE_JOBS: dict[int, JobState] = {}


def cancel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("ОТМЕНА", callback_data=CANCEL_CALLBACK_DATA)]])


async def _edit_status(status_msg, text: str, active: bool = True):
    try:
        await status_msg.edit_text(text, reply_markup=cancel_markup() if active else None)
    except Exception:
        pass


def _build_zip(webm_dir: Path, zip_path: Path, cancel_event: threading.Event | None = None) -> None:
    # Important: do not stop packing already-created files when cancel is set.
    # Cancel should affect the processing pipeline, but not make the partial archive empty.
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(webm_dir.glob("*.webm")):
            if not f.exists() or f.stat().st_size <= 0:
                continue
            z.write(f, f.name)


async def _send_zip_archive(
    update: Update,
    webm_dir: Path,
    zip_path: Path,
    filename: str,
    cancel_event: threading.Event | None = None,
) -> bool:
    await asyncio.to_thread(_build_zip, webm_dir, zip_path, cancel_event)
    if zip_path.exists() and zip_path.stat().st_size > 0:
        with zip_path.open("rb") as archive:
            await update.message.reply_document(archive, filename=filename)
        return True
    return False


def _format_summary(title: str, total: int, sent: int, skipped_items: list[tuple[str, str]]) -> str:
    lines = [
        title,
        f"Всего: {total}",
        f"Отправлено: {sent}",
        f"Пропущено: {len(skipped_items)}",
    ]

    if skipped_items:
        lines.append("")
        lines.append("Пропущенные:")
        for name, reason in skipped_items[:FINAL_SUMMARY_LIMIT]:
            lines.append(f"• {name} — {reason}")
        rest = len(skipped_items) - FINAL_SUMMARY_LIMIT
        if rest > 0:
            lines.append(f"… и ещё {rest}")

    return "\n".join(lines)


def _job_exists(chat_id: int) -> bool:
    return chat_id in _ACTIVE_JOBS


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return

    text = (update.message.text or "").strip()

    set_id = extract_set_id(text)
    if set_id:
        await handle_emote_set(update, context, set_id)
        return

    emote_id = extract_emote_id(text)
    if emote_id:
        await handle_single_emote(update, context, emote_id)


async def handle_emote_set(update: Update, context: ContextTypes.DEFAULT_TYPE, set_id: str):
    chat_id = update.effective_chat.id if update.effective_chat else update.effective_user.id
    if _job_exists(chat_id):
        await update.message.reply_text("⏳ Уже идёт обработка. Сначала нажми ОТМЕНА.")
        return

    cancel_event = threading.Event()
    status_msg = await update.message.reply_text("⏳ Подготовка...", reply_markup=cancel_markup())
    job = JobState(kind="set", cancel_event=cancel_event, status_msg=status_msg)
    _ACTIVE_JOBS[chat_id] = job

    task = asyncio.create_task(_process_emote_set_job(update, context, set_id, chat_id, job))
    job.task = task


async def handle_single_emote(update: Update, context: ContextTypes.DEFAULT_TYPE, emote_id: str):
    chat_id = update.effective_chat.id if update.effective_chat else update.effective_user.id
    if _job_exists(chat_id):
        await update.message.reply_text("⏳ Уже идёт обработка. Сначала нажми ОТМЕНА.")
        return

    cancel_event = threading.Event()
    status_msg = await update.message.reply_text("⏳ Подготовка...", reply_markup=cancel_markup())
    job = JobState(kind="single", cancel_event=cancel_event, status_msg=status_msg)
    _ACTIVE_JOBS[chat_id] = job

    task = asyncio.create_task(_process_single_emote_job(update, context, emote_id, chat_id, job))
    job.task = task


async def _process_emote_set_job(update: Update, context: ContextTypes.DEFAULT_TYPE, set_id: str, chat_id: int, job: JobState):
    status_msg = job.status_msg
    cancel_event = job.cancel_event
    work_dir = SAVE_ROOT / set_id
    work_dir.mkdir(exist_ok=True)
    webm_dir = work_dir / "telegram_emotes"
    webm_dir.mkdir(exist_ok=True)
    zip_path = SAVE_ROOT / f"{set_id}.zip"

    skipped_downloads: list[tuple[str, str]] = []
    skipped_convert: list[tuple[str, str]] = []
    skipped_download_count = 0
    skipped_convert_count = 0
    downloaded = 0
    converted = 0
    total = 0

    status_lock = asyncio.Lock()

    async def set_status(text: str, active: bool = True) -> None:
        async with status_lock:
            await _edit_status(status_msg, text, active=active)

    def render_status(phase: str, current: str) -> str:
        return (
            f"{phase}\n"
            f"Скачано: {downloaded}/{total}\n"
            f"Отправлено: {converted}/{total}\n"
            f"Пропущено: {skipped_download_count + skipped_convert_count}\n"
            f"Текущий: {current}"
        )

    try:
        data = await asyncio.to_thread(fetch_emote_list, set_id)
        if not data or "emotes" not in data:
            await set_status("Ошибка получения списка эмоутов.", active=False)
            return

        emotes = list(data["emotes"])
        total = len(emotes)
        queue: asyncio.Queue[tuple[str, Path] | None] = asyncio.Queue(maxsize=2)

        await set_status(render_status("📥 Скачивание эмоутов...", "—"))

        async def producer() -> None:
            nonlocal downloaded, skipped_download_count

            try:
                for index, emote in enumerate(emotes, 1):
                    if cancel_event.is_set():
                        break

                    emote_data = unwrap_emote(emote)
                    if not emote_data:
                        skipped_download_count += 1
                        skipped_downloads.append((f"эмоут #{index}", "не удалось прочитать данные"))
                        await set_status(render_status("📥 Скачивание эмоутов...", f"эмоут #{index}"))
                        continue

                    name = safe_name(emote_data.get("name", f"emote_{index}"))
                    emote_id = emote_data.get("id")
                    files = emote_data.get("host", {}).get("files", [])
                    best_file = get_best_file(files)

                    if not best_file or not emote_id:
                        skipped_download_count += 1
                        skipped_downloads.append((name, "нет WEBP-файла или id"))
                        await set_status(render_status("📥 Скачивание эмоутов...", name))
                        continue

                    await set_status(render_status("📥 Скачивание эмоутов...", name))

                    url = CDN_BASE.format(id=emote_id, file=best_file)
                    save_path = work_dir / f"{name}.webp"

                    ok = await asyncio.to_thread(download_file, url, save_path, cancel_event)
                    if ok:
                        downloaded += 1
                        await queue.put((name, save_path))
                    else:
                        if cancel_event.is_set():
                            break
                        skipped_download_count += 1
                        skipped_downloads.append((name, "ошибка скачивания"))

                    await set_status(render_status("📥 Скачивание эмоутов...", name))
            finally:
                await queue.put(None)

        async def consumer() -> None:
            nonlocal converted, skipped_convert_count

            while True:
                item = await queue.get()
                if item is None:
                    break

                name, webp_path = item
                if cancel_event.is_set():
                    if webp_path.exists():
                        try:
                            webp_path.unlink()
                        except Exception:
                            pass
                    continue

                await set_status(render_status("🎬 Конвертация в WEBM...", name))

                out_path = webm_dir / f"{webp_path.stem}.webm"
                try:
                    ok, reason = await asyncio.to_thread(convert_webp_to_webm, webp_path, out_path, cancel_event)
                except ConversionCancelled:
                    ok = False
                    reason = "отменено"
                except Exception as exc:
                    ok = False
                    reason = f"неожиданная ошибка: {exc}"

                if ok:
                    converted += 1
                else:
                    skipped_convert_count += 1
                    skipped_convert.append((name, reason or "неизвестная ошибка"))

                if webp_path.exists():
                    try:
                        webp_path.unlink()
                    except Exception:
                        pass

                await set_status(render_status("🎬 Конвертация в WEBM...", name))

        await asyncio.gather(producer(), consumer())

        webm_files = sorted(webm_dir.glob("*.webm"))
        skipped_items = skipped_downloads + skipped_convert

        if cancel_event.is_set():
            if not webm_files:
                await set_status(_format_summary("⛔ Отмена. Готовых WEBM нет.", total, 0, skipped_items), active=False)
                return

            await set_status("📦 Архивирую готовые WEBM...")
            sent = await _send_zip_archive(update, webm_dir, zip_path, f"{set_id}.zip", cancel_event=cancel_event)
            if sent:
                await set_status(
                    _format_summary("⛔ Отмена выполнена. Частичный архив отправлен.", total, len(webm_files), skipped_items),
                    active=False,
                )
            else:
                await set_status(_format_summary("⛔ Отмена. Архив не удалось собрать.", total, 0, skipped_items), active=False)
            return

        if converted <= 0:
            await set_status(_format_summary("Не удалось собрать итоговый файл.", total, 0, skipped_items), active=False)
            return

        await set_status(f"📦 Упаковываю архив...\nWEBM: {converted}\nПропущено: {skipped_convert_count}")
        sent = await _send_zip_archive(update, webm_dir, zip_path, f"{set_id}.zip", cancel_event=cancel_event)
        if not sent:
            await set_status(_format_summary("Не удалось собрать итоговый файл.", total, converted, skipped_items), active=False)
            return

        await set_status(_format_summary("✅ Готово!", total, converted, skipped_items), active=False)
    except Exception as exc:
        await _edit_status(status_msg, f"Ошибка обработки: {exc}", active=False)
    finally:
        _ACTIVE_JOBS.pop(chat_id, None)
        try:
            shutil.rmtree(work_dir)
        except Exception:
            pass
        try:
            zip_path.unlink()
        except Exception:
            pass


async def _process_single_emote_job(update: Update, context: ContextTypes.DEFAULT_TYPE, emote_id: str, chat_id: int, job: JobState):
    status_msg = job.status_msg
    cancel_event = job.cancel_event
    work_dir = SAVE_ROOT / emote_id
    work_dir.mkdir(exist_ok=True)
    zip_path = SAVE_ROOT / f"{emote_id}.zip"
    skipped_items: list[tuple[str, str]] = []

    try:
        payload = await asyncio.to_thread(fetch_emote, emote_id)
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

        download_ok = await asyncio.to_thread(download_file, url, save_path, cancel_event)
        if not download_ok:
            if cancel_event.is_set():
                summary = _format_summary("⛔ Отмена запрошена.", 1, 0, skipped_items)
                await _edit_status(status_msg, summary, active=False)
                return
            skipped_items.append((name, "ошибка скачивания"))
            summary = _format_summary("Не удалось скачать эмоут.", 1, 0, skipped_items)
            await _edit_status(status_msg, summary, active=False)
            return

        if cancel_event.is_set():
            await _edit_status(status_msg, "⛔ Отмена... Сохраняю то, что уже готово.")

        await _edit_status(
            status_msg,
            "🎬 Конвертация в WEBM...\nГотово: 0/1\nПропущено: 0\nТекущий: 1/1",
        )

        webm_dir, converted, skipped_convert_count, skipped_convert = await convert_to_telegram_format(
            work_dir,
            status_msg,
            cancel_event=cancel_event,
            reply_markup=cancel_markup(),
        )
        webm_files = sorted(webm_dir.glob("*.webm"))
        skipped_items.extend(skipped_convert)

        if cancel_event.is_set():
            if not webm_files:
                summary = _format_summary("⛔ Отмена. Готовых WEBM нет.", 1, 0, skipped_items)
                await _edit_status(status_msg, summary, active=False)
                return

            await _edit_status(status_msg, "📦 Архивирую готовый WEBM...")
            sent = await _send_zip_archive(update, webm_dir, zip_path, f"{name}.zip", cancel_event=cancel_event)
            if sent:
                summary = _format_summary(
                    "⛔ Отмена выполнена. Частичный архив отправлен.",
                    1,
                    len(webm_files),
                    skipped_items,
                )
                await _edit_status(status_msg, summary, active=False)
            else:
                summary = _format_summary("⛔ Отмена. Архив не удалось собрать.", 1, 0, skipped_items)
                await _edit_status(status_msg, summary, active=False)
            return

        if not webm_files:
            summary = _format_summary("Не удалось собрать итоговый файл.", 1, 0, skipped_items)
            await _edit_status(status_msg, summary, active=False)
            return

        result_file = webm_files[0]
        with result_file.open("rb") as f:
            await update.message.reply_document(f, filename=result_file.name)

        summary = _format_summary("✅ Готово!", 1, converted, skipped_items)
        await _edit_status(status_msg, summary, active=False)
    except Exception as exc:
        await _edit_status(status_msg, f"Ошибка обработки: {exc}", active=False)
    finally:
        _ACTIVE_JOBS.pop(chat_id, None)
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

    await query.answer()
    chat = query.message.chat if query.message else None
    if chat is None:
        return

    job = _ACTIVE_JOBS.get(chat.id)
    if job is None:
        try:
            await query.message.edit_text("⛔ Отмена недоступна: задача уже завершена.", reply_markup=None)
        except Exception:
            pass
        return

    job.cancel_event.set()

    try:
        await query.message.edit_text("⛔ Отмена запрошена...", reply_markup=None)
    except Exception:
        pass


def about_text() -> str:
    return "Бот запущен."


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(about_text())
