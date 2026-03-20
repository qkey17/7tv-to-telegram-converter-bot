from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters

from config import BOT_TOKEN
from bot.handlers import handle_message, about_command


def main():
    # === Инициализация приложения ===
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # === Регистрация команд ===
    # /about — информация о боте
    app.add_handler(CommandHandler("about", about_command))

    # === Обработка обычных сообщений ===
    # Любой текст, который НЕ является командой
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # === Запуск бота ===
    print("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()