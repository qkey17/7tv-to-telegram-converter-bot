from telegram import BotCommand
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from bot.handlers import about_command, handle_cancel, handle_message, CANCEL_CALLBACK_DATA
from config import BOT_TOKEN


async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("about", "О боте"),
    ])


def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("about", about_command))
    app.add_handler(CallbackQueryHandler(handle_cancel, pattern=f"^{CANCEL_CALLBACK_DATA}$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
