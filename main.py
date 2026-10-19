import asyncio
import logging
import sys
import shutil
from telegram.ext import Application
from config_manager import state, CONFIG_PATH, BACKUP_DIR
from telegram_ui import is_admin, cmd_start, callback_router, text_input_handler
from discord_client import promotion_loop
from telegram.ext import CommandHandler, CallbackQueryHandler, MessageHandler, filters

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("AutoPromo")

# =============================================================
#  MAIN
# =============================================================
async def post_init(application: Application):
    admin_id = state.config.get("telegram_admin_id")
    try:
        n = len(state.config.get("accounts", []))
        await application.bot.send_message(
            chat_id=admin_id,
            text=(
                "✅ *Bot Online!*\n"
                f"Discord Auto-Promotion siap.\n"
                f"📝 {n} akun terdaftar.\n\n"
                "Ketik /start untuk membuka Control Panel."
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning("Gagal kirim pesan startup: %s", e)


def main():
    # Auto-restore: jika config.json tidak ada, coba restore dari backup atau file contoh
    if not CONFIG_PATH.exists():
        backups = sorted(BACKUP_DIR.glob("config_*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if BACKUP_DIR.exists() else []
        example_path = Path(__file__).parent / "config.example.json"
        exml_path = Path(__file__).parent / "config.exml.json"
        if backups:
            latest = backups[0]
            shutil.copy2(latest, CONFIG_PATH)
            logger.info("⚡ config.json tidak ditemukan — di-restore dari backup: %s", latest.name)
        elif example_path.exists():
            shutil.copy2(example_path, CONFIG_PATH)
            logger.info("⚡ config.json tidak ditemukan — dibuat baru dari template config.example.json")
        elif exml_path.exists():
            shutil.copy2(exml_path, CONFIG_PATH)
            logger.info("⚡ config.json tidak ditemukan — dibuat baru dari template config.exml.json")
        else:
            logger.error("❌ config.json tidak ditemukan dan tidak ada backup atau template!")
            return

    state.load_config()
    tg_token = state.config.get("telegram_bot_token", "")

    if not tg_token or tg_token.startswith("ISI_"):
        logger.error("❌ Telegram Bot Token belum diisi!")
        return

    admin_id = state.config.get("telegram_admin_id", 0)
    if admin_id == 0 or admin_id == 123456789:
        logger.error("❌ Telegram Admin ID belum diisi!")
        return

    app = Application.builder().token(tg_token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input_handler))

    logger.info("═══════════════════════════════════════════════")
    logger.info("  Discord Auto-Promotion Bot — STARTED")
    logger.info("  Admin ID: %s", admin_id)
    logger.info("  Akun: %d", len(state.config.get("accounts", [])))
    logger.info("═══════════════════════════════════════════════")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
