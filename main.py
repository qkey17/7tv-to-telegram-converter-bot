from telegram import BotCommand
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters

from config import BOT_TOKEN
from bot.handlers import handle_message, about_command


# === Установка команд (меню в Telegram) ===
async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("about", "О боте"),
    ])


def main():
    # === Инициализация приложения ===
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # === Регистрация команд ===
    app.add_handler(CommandHandler("about", about_command))

    # === Обработка обычных сообщений ===
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # === Запуск бота ===
    print("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
