import asyncio
import json
import logging
import random
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

from config_manager import state
from utils import parse_spintax

# =============================================================
#  DISCORD SENDER  (httpx – direct REST API)
# =============================================================
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
DISCORD_API = "https://discord.com/api/v9"


def build_headers(token: str) -> dict:
    """Buat headers Discord yang menyerupai browser asli."""
    return {
        "Authorization": token,
        "Content-Type": "application/json",
        "User-Agent": CHROME_USER_AGENT,
        "X-Super-Properties": (
            "eyJvcyI6IldpbmRvd3MiLCJicm93c2VyIjoiQ2hyb21lIiwiZGV2a"
            "WNlIjoiIiwic3lzdGVtX2xvY2FsZSI6ImVuLVVTIiwiYnJvd3Nlcl"
            "91c2VyX2FnZW50IjoiTW96aWxsYS81LjAgKFdpbmRvd3MgTlQgMT"
            "AuMDsgV2luNjQ7IHg2NCkgQXBwbGVXZWJLaXQvNTM3LjM2IChLSF"
            "RNTCwgbGlrZSBHZWNrbykgQ2hyb21lLzEyNC4wLjAuMCBTYWZhcmk"
            "vNTM3LjM2In0="
        ),
    }


async def send_discord_message(
    client: httpx.AsyncClient,
    channel_id: str,
    message: str,
    headers: dict,
) -> dict:
    """Kirim pesan ke channel Discord via REST API."""
    url = f"{DISCORD_API}/channels/{channel_id}/messages"
    payload = {"content": message}
    try:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code == 200:
            return {"success": True, "status_code": 200, "detail": "OK"}
        else:
            return {"success": False, "status_code": resp.status_code, "detail": resp.text[:200]}
    except httpx.RequestError as exc:
        return {"success": False, "status_code": 0, "detail": str(exc)[:200]}


async def validate_discord_token(token: str, proxy: str = None) -> dict:
    """Validasi Discord token dengan GET /users/@me."""
    headers = build_headers(token)
    transport = httpx.AsyncHTTPTransport(proxy=proxy) if proxy else None
    try:
        async with httpx.AsyncClient(transport=transport, timeout=15) as client:
            resp = await client.get(f"{DISCORD_API}/users/@me", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "valid": True,
                    "username": data.get("username", "?"),
                    "discriminator": data.get("discriminator", "0"),
                    "id": data.get("id", "?"),
                }
            else:
                return {"valid": False, "status_code": resp.status_code}
    except Exception as e:
        return {"valid": False, "error": str(e)[:100]}


# =============================================================
#  PROMOTION LOOP  (satu coroutine per akun Discord)
# =============================================================
async def promotion_loop(account: dict, tg_app: Application):
    """Loop pengiriman pesan untuk satu akun Discord."""
    name = account["name"]
    token = account["token"]
    proxy = account.get("proxy")
    channels = account.get("channels", [])
    headers = build_headers(token)

    transport = httpx.AsyncHTTPTransport(proxy=proxy) if proxy else None

    async with httpx.AsyncClient(
        transport=transport, timeout=httpx.Timeout(30.0), follow_redirects=True
    ) as client:
        state.stats[name]["active"] = True
        logger.info("[%s] Promotion loop dimulai (sistem scheduling asinkron)", name)

        # Dictionary to keep track of next run time for each channel
        next_run_times: dict[str, float] = {}

        while state.running:
            # Re-read account data dari config (bisa berubah real-time)
            current_acc = None
            for a in state.config.get("accounts", []):
                if a["name"] == name:
                    current_acc = a
                    break
            if current_acc is None:
                break  # akun dihapus

            channels = current_acc.get("channels", [])
            if not channels:
                await asyncio.sleep(10)
                continue

            now = time.time()
            message_sent_in_this_loop = False

            for ch_item in channels:
                if not state.running:
                    break

                if isinstance(ch_item, dict):
                    ch_id = ch_item.get("id")
                    ch_msg = ch_item.get("message", "").strip()
                    ch_delay_minutes = ch_item.get("delay")
                    raw_msg = ch_msg if ch_msg else current_acc.get("message", "Hello!")
                    ch_name_label = ch_item.get("name", "")
                else:
                    ch_id = ch_item
                    raw_msg = current_acc.get("message", "Hello!")
                    ch_delay_minutes = None
                    ch_name_label = ""
                    
                # Gunakan fallback delay jika tidak ada delay per channel
                if ch_delay_minutes is None:
                    ch_delay_minutes = state.config.get("default_delay_minutes", 60)

                ch_display = f"{ch_name_label} ({ch_id})" if ch_name_label else f"{ch_id}"

                # Init next run time to NOW (0) jika baru didapat
                if ch_id not in next_run_times:
                    next_run_times[ch_id] = 0

                if now >= next_run_times[ch_id]:
                    # Jika ada channel sebelumnya di loop yg juga kirim, beri jeda aman agar tak ratelimit
                    if message_sent_in_this_loop:
                        await asyncio.sleep(random.uniform(3, 8))
                    
                    final_msg = parse_spintax(raw_msg)
                    max_retries = 3
                    
                    for attempt in range(max_retries):
                        result = await send_discord_message(client, ch_id, final_msg, headers)
                        message_sent_in_this_loop = True

                        if result["success"]:
                            state.stats[name]["total_sent"] += 1
                            state.stats[name]["last_channel"] = ch_id
                            state.stats[name]["last_time"] = time.strftime("%H:%M:%S")
                            logger.info("[%s] ✅ Terkirim ke #%s", name, ch_id)
                            break
                        
                        code = result["status_code"]
                        detail = result["detail"]
                        
                        if code == 429:
                            try:
                                wait = json.loads(detail).get("retry_after", 30)
                            except Exception:
                                wait = 30
                                
                            logger.warning("[%s] ⏳ Rate Limit #%s, menunggu %ss lalu retry...", name, ch_id, wait)
                            if attempt == 0:
                                alert = f"⏳ *Rate Limited*\nAkun: `{name}`\nMenunggu {wait}s lalu mencoba ulang..."
                                try:
                                    admin_id = state.config.get("telegram_admin_id")
                                    await tg_app.bot.send_message(chat_id=admin_id, text=alert, parse_mode="Markdown")
                                except Exception:
                                    pass
                                    
                            await asyncio.sleep(wait + 1)
                        else:
                            state.stats[name]["total_failed"] += 1
                            logger.warning("[%s] ❌ Gagal ke #%s (HTTP %s)", name, ch_id, code)

                            alert = ""
                            if code == 401:
                                alert = f"🚨 *Token Mati!*\nAkun: `{name}`\nHTTP 401 Unauthorized"
                            elif code == 403:
                                alert = f"⚠️ *Akses Ditolak*\nAkun: `{name}`\nChannel: `{ch_display}`"
                            elif code == 404:
                                alert = f"⚠️ *Channel Tidak Ditemukan*\nAkun: `{name}`\nChannel: `{ch_display}`"
                            else:
                                alert = f"❗ *Error Mengirim Pesan*\nAkun: `{name}`\nChannel: `{ch_display}`\nHTTP {code}\n`{detail[:100]}`"

                            if alert:
                                try:
                                    admin_id = state.config.get("telegram_admin_id")
                                    await tg_app.bot.send_message(chat_id=admin_id, text=alert, parse_mode="Markdown")
                                except Exception:
                                    pass
                            break
                    else:
                        # Jika setelah 3 kali retry masih kena spam block
                        state.stats[name]["total_failed"] += 1
                        logger.warning("[%s] ❌ Tetap gagal ke #%s setelah di-retry %d kali.", name, ch_id, max_retries)

                    # Reschedule this channel
                    base = float(ch_delay_minutes) * 60
                    variation = state.config.get("delay_variation_percent", 10) / 100
                    actual_delay = random.uniform(base * (1 - variation), base * (1 + variation))
                    next_run_times[ch_id] = time.time() + actual_delay
                    logger.info("[%s] 💤 #%s dijadwalkan ulang %.1f menit lagi.", name, ch_id, actual_delay / 60)

            # Polling general sleep loop (check each 5s or when cancelled)
            if state.running:
                await asyncio.sleep(5)

    state.stats[name]["active"] = False
    logger.info("[%s] Promotion loop berhenti.", name)


