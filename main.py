from telegram.ext import ApplicationBuilder, MessageHandler, filters
from config import BOT_TOKEN
from bot.handlers import handle_message

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot started")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()