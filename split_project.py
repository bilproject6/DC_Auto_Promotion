import os
import re

source = "main.py"
with open(source, "r", encoding="utf-8") as f:
    text = f.read()

# We need to extract four sections: 
# 1. State config logic -> config_manager.py (We already wrote this correctly, we will just keep it)
# 2. Discord REST and promotion_loop -> discord_client.py
# 3. Telegram Keyboards and Handlers -> telegram_ui.py
# 4. main() loop -> main.py

# To be perfectly safe instead of regex matching the whole big file, 
# I will just write a python script to grab the sections.

# Section: Discord Sender (from start of DISCORD SENDER to GLOBAL STATE)
discord_sender_idx = text.find("# =============================================================\n#  DISCORD SENDER")
global_state_idx = text.find("# =============================================================\n#  GLOBAL STATE")
promotion_loop_idx = text.find("# =============================================================\n#  PROMOTION LOOP")
telegram_bot_idx = text.find("# =============================================================\n#  TELEGRAM BOT")
main_idx = text.find("# =============================================================\n#  MAIN")

# Extracted blocks
import_block = """import asyncio\nimport json\nimport logging\nimport random\nimport re\nimport shutil\nimport time\nfrom datetime import datetime\nfrom pathlib import Path\nfrom typing import Optional\n\nimport httpx\nfrom telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update\nfrom telegram.ext import (\n    Application,\n    CallbackQueryHandler,\n    CommandHandler,\n    ContextTypes,\n    MessageHandler,\n    filters,\n)\n\nlogger = logging.getLogger(__name__)\n\n"""

discord_client_content = import_block + "from config_manager import state\nfrom utils import parse_spintax\n\n" + \
    text[discord_sender_idx:global_state_idx] + \
    text[promotion_loop_idx:telegram_bot_idx]

telegram_ui_content = import_block + "from config_manager import state\nfrom utils import parse_spintax\nfrom discord_client import send_discord_message, validate_discord_token, promotion_loop\n\n" + \
    text[telegram_bot_idx:main_idx]

main_content = """import asyncio\nimport logging\nimport sys\nfrom telegram.ext import Application\nfrom config_manager import state\nfrom telegram_ui import is_admin, start_cmd, button_click_handler, text_input_handler\nfrom discord_client import promotion_loop\nfrom telegram.ext import CommandHandler, CallbackQueryHandler, MessageHandler, filters\n\nlogging.basicConfig(\n    level=logging.INFO,\n    format="%(asctime)s │ %(levelname)-7s │ %(message)s",\n    datefmt="%Y-%m-%d %H:%M:%S",\n)\nlogger = logging.getLogger("AutoPromo")\n\n""" + \
    text[main_idx:]

# But wait, in telegram_ui_content, setup_handlers is not defined. We do it in main.py directly or add it.
# Let's adjust main_content:
main_content = main_content.replace(
    "application.add_handler(CommandHandler(\"start\", start_cmd))",
    "application.add_handler(CommandHandler(\"start\", start_cmd))\n    application.add_handler(CallbackQueryHandler(button_click_handler))\n    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input_handler))"
)

with open("discord_client.py", "w", encoding="utf-8") as f:
    f.write(discord_client_content)

with open("telegram_ui.py", "w", encoding="utf-8") as f:
    f.write(telegram_ui_content)

with open("main_refactored.py", "w", encoding="utf-8") as f:
    f.write(main_content)

print("Split completed successfully.")
