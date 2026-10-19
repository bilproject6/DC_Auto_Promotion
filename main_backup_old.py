"""
=============================================================
  Discord Multi-Account Auto-Promotion
  Controlled via Telegram Bot (Inline Keyboard)
  
  Arsitektur: asyncio + httpx + python-telegram-bot v20
  
  Fitur:
  - Full account management via Telegram
  - Spintax message parser
  - Randomized delay & anti-ban
  - Proxy support per-akun
  - Error handling & notification
=============================================================
"""

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

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("AutoPromo")

# ── Path ke config.json & backup ─────────────────────────────
CONFIG_PATH = Path(__file__).parent / "config.json"
BACKUP_DIR = Path(__file__).parent / "backups"
MAX_BACKUPS = 5  # simpan maksimal 5 backup terbaru


# =============================================================
#  SPINTAX PARSER
# =============================================================
def parse_spintax(text: str) -> str:
    """Proses spintax secara rekursif. Mendukung nested: {A|{B|C}}."""
    pattern = re.compile(r"\{([^{}]+)\}")
    while pattern.search(text):
        text = pattern.sub(lambda m: random.choice(m.group(1).split("|")), text)
    return text


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
#  GLOBAL STATE
# =============================================================
class PromoState:
    """Menyimpan state seluruh sistem."""

    def __init__(self):
        self.running: bool = False
        self.tasks: list[asyncio.Task] = []
        self.config: dict = {}
        self.stats: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    def load_config(self):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            self.config = json.load(f)

        # Migrate config to support per-channel messages and names
        migrated = False
        for acc in self.config.get("accounts", []):
            channels = acc.get("channels", [])
            for i, ch in enumerate(channels):
                if isinstance(ch, str):
                    channels[i] = {"id": ch, "name": "", "message": ""}
                    migrated = True
                elif isinstance(ch, dict) and "name" not in ch:
                    ch["name"] = ""
                    migrated = True

        if migrated:
            self.save_config()

        for acc in self.config.get("accounts", []):
            name = acc["name"]
            if name not in self.stats:
                self.stats[name] = {
                    "total_sent": 0,
                    "total_failed": 0,
                    "last_channel": "-",
                    "last_time": "-",
                    "active": False,
                }
        logger.info("Config loaded: %d akun", len(self.config.get("accounts", [])))

    def save_config(self):
        """Simpan config + auto-backup."""
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=4, ensure_ascii=False)
        # Auto-backup setiap kali save
        self.create_backup(auto=True)

    # ── Backup & Restore ─────────────────────────────────────
    def create_backup(self, auto: bool = False) -> str:
        """Buat backup config.json. Return: nama file backup."""
        BACKUP_DIR.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = "auto" if auto else "manual"
        backup_name = f"config_{prefix}_{timestamp}.json"
        backup_path = BACKUP_DIR / backup_name
        shutil.copy2(CONFIG_PATH, backup_path)
        logger.info("Backup dibuat: %s", backup_name)
        # Bersihkan backup lama (simpan MAX_BACKUPS terbaru)
        self._cleanup_old_backups()
        return backup_name

    def _cleanup_old_backups(self):
        """Hapus backup terlama jika melebihi MAX_BACKUPS."""
        if not BACKUP_DIR.exists():
            return
        backups = sorted(BACKUP_DIR.glob("config_*.json"), key=lambda p: p.stat().st_mtime)
        while len(backups) > MAX_BACKUPS:
            oldest = backups.pop(0)
            oldest.unlink()
            logger.info("Backup lama dihapus: %s", oldest.name)

    def list_backups(self) -> list[dict]:
        """List semua backup yang tersedia."""
        if not BACKUP_DIR.exists():
            return []
        backups = sorted(BACKUP_DIR.glob("config_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        result = []
        for bp in backups:
            mtime = datetime.fromtimestamp(bp.stat().st_mtime)
            # Baca jumlah akun di backup
            try:
                with open(bp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                n_accounts = len(data.get("accounts", []))
            except Exception:
                n_accounts = "?"
            result.append({
                "filename": bp.name,
                "path": bp,
                "time": mtime.strftime("%d/%m/%Y %H:%M:%S"),
                "size": f"{bp.stat().st_size / 1024:.1f} KB",
                "accounts": n_accounts,
            })
        return result

    def restore_backup(self, filename: str) -> bool:
        """Restore config.json dari file backup."""
        backup_path = BACKUP_DIR / filename
        if not backup_path.exists():
            return False
        # Backup config saat ini sebelum restore (safety net)
        self.create_backup(auto=False)
        shutil.copy2(backup_path, CONFIG_PATH)
        self.load_config()
        logger.info("Config di-restore dari: %s", filename)
        return True

    def get_delay_range(self) -> tuple[float, float]:
        base = self.config.get("default_delay_minutes", 60) * 60
        variation = self.config.get("delay_variation_percent", 10) / 100
        return base * (1 - variation), base * (1 + variation)

    def get_random_delay(self) -> float:
        lo, hi = self.get_delay_range()
        return random.uniform(lo, hi)


state = PromoState()


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
                    result = await send_discord_message(client, ch_id, final_msg, headers)
                    message_sent_in_this_loop = True

                    if result["success"]:
                        state.stats[name]["total_sent"] += 1
                        state.stats[name]["last_channel"] = ch_id
                        state.stats[name]["last_time"] = time.strftime("%H:%M:%S")
                        logger.info("[%s] ✅ Terkirim ke #%s", name, ch_id)
                    else:
                        state.stats[name]["total_failed"] += 1
                        code = result["status_code"]
                        detail = result["detail"]
                        logger.warning("[%s] ❌ Gagal ke #%s (HTTP %s)", name, ch_id, code)

                        alert = ""
                        if code == 401:
                            alert = f"🚨 *Token Mati!*\nAkun: `{name}`\nHTTP 401 Unauthorized"
                        elif code == 403:
                            alert = f"⚠️ *Akses Ditolak*\nAkun: `{name}`\nChannel: `{ch_display}`"
                        elif code == 404:
                            alert = f"⚠️ *Channel Tidak Ditemukan*\nAkun: `{name}`\nChannel: `{ch_display}`"
                        elif code == 429:
                            try:
                                wait = json.loads(detail).get("retry_after", 30)
                            except Exception:
                                wait = 30
                            alert = f"⏳ *Rate Limited*\nAkun: `{name}`\nMenunggu {wait}s..."
                            await asyncio.sleep(wait)
                        else:
                            alert = f"❗ *Error Mengirim Pesan*\nAkun: `{name}`\nChannel: `{ch_display}`\nHTTP {code}\n`{detail[:100]}`"

                        if alert:
                            try:
                                admin_id = state.config.get("telegram_admin_id")
                                await tg_app.bot.send_message(
                                    chat_id=admin_id, text=alert, parse_mode="Markdown"
                                )
                            except Exception:
                                pass

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


# =============================================================
#  TELEGRAM BOT — KEYBOARDS
# =============================================================
def is_admin(update: Update) -> bool:
    admin_id = state.config.get("telegram_admin_id", 0)
    return update.effective_user.id == admin_id


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚀 Start All", callback_data="start_all"),
            InlineKeyboardButton("🛑 Stop All", callback_data="stop_all"),
        ],
        [
            InlineKeyboardButton("📊 Status", callback_data="status"),
            InlineKeyboardButton("⚙️ Settings", callback_data="settings"),
        ],
        [
            InlineKeyboardButton("👤 Kelola Akun", callback_data="manage_accounts"),
        ],
    ])


def settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏱ Ubah Delay", callback_data="set_delay")],
        [InlineKeyboardButton("💾 Backup & Restore", callback_data="backup_menu")],
        [InlineKeyboardButton("🔄 Reload Config", callback_data="reload_config")],
        [InlineKeyboardButton("◀️ Kembali", callback_data="back_main")],
    ])


def backup_menu_keyboard() -> InlineKeyboardMarkup:
    """Keyboard menu backup & restore."""
    backups = state.list_backups()
    buttons = [
        [InlineKeyboardButton("💾 Buat Backup Sekarang", callback_data="backup_create")],
    ]
    if backups:
        for i, bp in enumerate(backups):
            label = f"📁 {bp['time']} ({bp['accounts']} akun)"
            buttons.append([InlineKeyboardButton(label, callback_data=f"backup_view_{i}")])
    else:
        buttons.append([InlineKeyboardButton("ℹ️ Belum ada backup", callback_data="backup_menu")])
    buttons.append([InlineKeyboardButton("◀️ Kembali", callback_data="settings")])
    return InlineKeyboardMarkup(buttons)


def accounts_menu_keyboard() -> InlineKeyboardMarkup:
    """Keyboard daftar akun + tombol tambah."""
    buttons = []
    for i, acc in enumerate(state.config.get("accounts", [])):
        name = acc.get("name", f"Akun {i}")
        ch_count = len(acc.get("channels", []))
        buttons.append([
            InlineKeyboardButton(f"👤 {name} ({ch_count} ch)", callback_data=f"acc_view_{i}")
        ])
    buttons.append([InlineKeyboardButton("➕ Tambah Akun Baru", callback_data="acc_add")])
    buttons.append([InlineKeyboardButton("◀️ Kembali", callback_data="back_main")])
    return InlineKeyboardMarkup(buttons)


def account_detail_keyboard(idx: int) -> InlineKeyboardMarkup:
    """Keyboard detail & aksi untuk satu akun."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔑 Ubah Token", callback_data=f"acc_token_{idx}"),
            InlineKeyboardButton("✏️ Ubah Nama", callback_data=f"acc_name_{idx}"),
        ],
        [
            InlineKeyboardButton("🌐 Ubah Proxy", callback_data=f"acc_proxy_{idx}"),
            InlineKeyboardButton("💬 Ubah Pesan", callback_data=f"acc_msg_{idx}"),
        ],
        [
            InlineKeyboardButton("📺 Kelola Channels", callback_data=f"acc_channels_{idx}"),
        ],
        [
            InlineKeyboardButton("🔍 Validasi Token", callback_data=f"acc_validate_{idx}"),
        ],
        [
            InlineKeyboardButton("🗑 Hapus Akun", callback_data=f"acc_delete_{idx}"),
        ],
        [
            InlineKeyboardButton("◀️ Kembali", callback_data="manage_accounts"),
        ],
    ])


def channels_keyboard(acc_idx: int) -> InlineKeyboardMarkup:
    """Keyboard kelola channels untuk satu akun."""
    acc = state.config.get("accounts", [])[acc_idx]
    buttons = []
    for j, ch_item in enumerate(acc.get("channels", [])):
        if isinstance(ch_item, dict):
            ch_id = ch_item.get("id", "?")
            ch_name = ch_item.get("name", "")
            has_msg = "💬 " if ch_item.get("message", "").strip() else ""
            display_text = f"{has_msg}{ch_name} ({ch_id})" if ch_name else f"{has_msg}{ch_id}"
        else:
            ch_id = ch_item
            display_text = f"{ch_id}"
            
        buttons.append([
            InlineKeyboardButton(f"⚙️ {display_text}", callback_data=f"ch_view_{acc_idx}_{j}")
        ])
    buttons.append([InlineKeyboardButton("➕ Tambah Channel", callback_data=f"ch_add_{acc_idx}")])
    buttons.append([InlineKeyboardButton("◀️ Kembali", callback_data=f"acc_view_{acc_idx}")])
    return InlineKeyboardMarkup(buttons)

def channel_detail_keyboard(acc_idx: int, ch_idx: int) -> InlineKeyboardMarkup:
    """Keyboard detail channel."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Ubah Nama Channel", callback_data=f"ch_name_{acc_idx}_{ch_idx}")],
        [InlineKeyboardButton("💬 Ubah Pesan Channel", callback_data=f"ch_msg_{acc_idx}_{ch_idx}")],
        [InlineKeyboardButton("⏱ Ubah Delay Channel", callback_data=f"ch_delay_{acc_idx}_{ch_idx}")],
        [InlineKeyboardButton("🗑 Hapus Channel", callback_data=f"ch_remove_{acc_idx}_{ch_idx}")],
        [InlineKeyboardButton("◀️ Kembali", callback_data=f"acc_channels_{acc_idx}")]
    ])


# =============================================================
#  TELEGRAM BOT — HANDLERS
# =============================================================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Anda tidak memiliki akses.")
        return
    n_accounts = len(state.config.get("accounts", []))
    status = "🟢 Berjalan" if state.running else "🔴 Berhenti"
    await update.message.reply_text(
        f"🤖 *Discord Auto-Promotion Control Panel*\n\n"
        f"Status: {status}\n"
        f"Akun: `{n_accounts}` terdaftar\n\n"
        f"Pilih menu:",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown",
    )


async def callback_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(update):
        await query.edit_message_text("⛔ Akses ditolak.")
        return

    data = query.data

    # ═══════════════════════════════════════════════════════════
    #  MAIN MENU ACTIONS
    # ═══════════════════════════════════════════════════════════

    # ── START ALL ────────────────────────────────────────────
    if data == "start_all":
        if state.running:
            await query.edit_message_text(
                "⚠️ Promosi sudah berjalan!", reply_markup=main_menu_keyboard()
            )
            return

        # Cek ada akun valid
        accounts = state.config.get("accounts", [])
        valid_accounts = [a for a in accounts if a.get("token") and not a["token"].startswith("ISI_")]
        if not valid_accounts:
            await query.edit_message_text(
                "❌ *Tidak ada akun dengan token valid!*\n\n"
                "Tambahkan akun via 👤 Kelola Akun terlebih dahulu.",
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown",
            )
            return

        state.running = True
        state.load_config()

        tg_app = ctx.application
        for acc in valid_accounts:
            task = asyncio.create_task(promotion_loop(acc, tg_app))
            state.tasks.append(task)

        await query.edit_message_text(
            f"🚀 *Promosi Dimulai!*\n"
            f"• {len(state.tasks)} akun aktif\n"
            f"• Delay: {state.config.get('default_delay_minutes', 60)} menit "
            f"(±{state.config.get('delay_variation_percent', 10)}%)",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown",
        )

    # ── STOP ALL ─────────────────────────────────────────────
    elif data == "stop_all":
        if not state.running:
            await query.edit_message_text(
                "ℹ️ Tidak ada promosi yang berjalan.", reply_markup=main_menu_keyboard()
            )
            return

        state.running = False
        for t in state.tasks:
            t.cancel()
        await asyncio.gather(*state.tasks, return_exceptions=True)
        state.tasks.clear()

        await query.edit_message_text(
            "🛑 *Semua promosi dihentikan.*",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown",
        )

    # ── STATUS ───────────────────────────────────────────────
    elif data == "status":
        if not state.stats:
            await query.edit_message_text(
                "ℹ️ Belum ada data. Mulai promosi terlebih dahulu.",
                reply_markup=main_menu_keyboard(),
            )
            return

        lines = ["📊 *STATUS LAPORAN*\n"]
        for acc_name, s in state.stats.items():
            icon = "🟢" if s["active"] else "🔴"
            lines.append(
                f"{icon} *{acc_name}*\n"
                f"   Terkirim: `{s['total_sent']}` │ Gagal: `{s['total_failed']}`\n"
                f"   Ch terakhir: `{s['last_channel']}`\n"
                f"   Waktu: `{s['last_time']}`\n"
            )
        total_s = sum(s["total_sent"] for s in state.stats.values())
        total_f = sum(s["total_failed"] for s in state.stats.values())
        lines.append(f"─────────────────\n📬 Total: `{total_s}` terkirim, `{total_f}` gagal")

        await query.edit_message_text(
            "\n".join(lines), reply_markup=main_menu_keyboard(), parse_mode="Markdown"
        )

    # ── SETTINGS ─────────────────────────────────────────────
    elif data == "settings":
        lo, hi = state.get_delay_range()
        await query.edit_message_text(
            f"⚙️ *SETTINGS*\n\n"
            f"⏱ Delay: `{state.config.get('default_delay_minutes', 60)}` menit "
            f"(±{state.config.get('delay_variation_percent', 10)}%)\n"
            f"   Range: `{lo/60:.1f}` – `{hi/60:.1f}` menit\n\n"
            f"📝 Akun: `{len(state.config.get('accounts', []))}`",
            reply_markup=settings_keyboard(),
            parse_mode="Markdown",
        )

    elif data == "set_delay":
        ctx.user_data["awaiting"] = "delay"
        await query.edit_message_text(
            "⏱ *Ubah Delay*\n\n"
            "Kirim angka delay baru (dalam menit).\n"
            "Contoh: `45` untuk 45 menit.\n\n"
            "Kirim `batal` untuk membatalkan.",
            parse_mode="Markdown",
        )

    elif data == "reload_config":
        try:
            state.load_config()
            await query.edit_message_text(
                f"🔄 *Config berhasil di-reload!*\n"
                f"📝 {len(state.config.get('accounts', []))} akun ditemukan.",
                reply_markup=settings_keyboard(),
                parse_mode="Markdown",
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ Gagal reload config:\n`{e}`",
                reply_markup=settings_keyboard(),
                parse_mode="Markdown",
            )

    # ═══════════════════════════════════════════════════════════
    #  BACKUP & RESTORE
    # ═══════════════════════════════════════════════════════════

    elif data == "backup_menu":
        backups = state.list_backups()
        if backups:
            text = f"💾 *Backup & Restore*\n\n📂 {len(backups)} backup tersedia:\n"
            text += "(Auto-backup dibuat setiap perubahan config)\n\n"
            text += "Tap backup untuk melihat detail & restore:"
        else:
            text = "💾 *Backup & Restore*\n\nBelum ada backup."
        await query.edit_message_text(
            text, reply_markup=backup_menu_keyboard(), parse_mode="Markdown"
        )

    elif data == "backup_create":
        try:
            name = state.create_backup(auto=False)
            await query.edit_message_text(
                f"✅ *Backup manual berhasil dibuat!*\n\n"
                f"📁 File: `{name}`",
                reply_markup=backup_menu_keyboard(),
                parse_mode="Markdown",
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ Gagal membuat backup:\n`{e}`",
                reply_markup=backup_menu_keyboard(),
                parse_mode="Markdown",
            )

    elif data.startswith("backup_view_"):
        idx = int(data.replace("backup_view_", ""))
        backups = state.list_backups()
        if idx >= len(backups):
            await query.edit_message_text(
                "❌ Backup tidak ditemukan.", reply_markup=backup_menu_keyboard()
            )
            return
        bp = backups[idx]
        await query.edit_message_text(
            f"📁 *Detail Backup*\n\n"
            f"File: `{bp['filename']}`\n"
            f"Waktu: `{bp['time']}`\n"
            f"Ukuran: `{bp['size']}`\n"
            f"Akun: `{bp['accounts']}`\n\n"
            "⚠️ Restore akan mengganti config saat ini."
            "\n(Config saat ini akan di-backup dulu sebelum restore)",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Restore", callback_data=f"backup_restore_{idx}"),
                    InlineKeyboardButton("🗑 Hapus", callback_data=f"backup_delete_{idx}"),
                ],
                [InlineKeyboardButton("◀️ Kembali", callback_data="backup_menu")],
            ]),
            parse_mode="Markdown",
        )

    elif data.startswith("backup_restore_"):
        idx = int(data.replace("backup_restore_", ""))
        backups = state.list_backups()
        if idx >= len(backups):
            await query.edit_message_text(
                "❌ Backup tidak ditemukan.", reply_markup=backup_menu_keyboard()
            )
            return
        bp = backups[idx]
        success = state.restore_backup(bp["filename"])
        if success:
            n = len(state.config.get("accounts", []))
            await query.edit_message_text(
                f"✅ *Config berhasil di-restore!*\n\n"
                f"📁 Dari: `{bp['filename']}`\n"
                f"📝 {n} akun dimuat.",
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                "❌ Gagal restore. File backup mungkin rusak.",
                reply_markup=backup_menu_keyboard(),
            )

    elif data.startswith("backup_delete_"):
        idx = int(data.replace("backup_delete_", ""))
        backups = state.list_backups()
        if idx < len(backups):
            bp = backups[idx]
            try:
                bp["path"].unlink()
                await query.edit_message_text(
                    f"🗑 Backup `{bp['filename']}` dihapus.",
                    reply_markup=backup_menu_keyboard(),
                    parse_mode="Markdown",
                )
            except Exception as e:
                await query.edit_message_text(
                    f"❌ Gagal hapus: `{e}`",
                    reply_markup=backup_menu_keyboard(),
                    parse_mode="Markdown",
                )
        else:
            await query.edit_message_text(
                "❌ Backup tidak ditemukan.", reply_markup=backup_menu_keyboard()
            )

    elif data == "back_main":
        n_accounts = len(state.config.get("accounts", []))
        status = "🟢 Berjalan" if state.running else "🔴 Berhenti"
        await query.edit_message_text(
            f"🤖 *Discord Auto-Promotion Control Panel*\n\n"
            f"Status: {status}\n"
            f"Akun: `{n_accounts}` terdaftar\n\nPilih menu:",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown",
        )

    # ═══════════════════════════════════════════════════════════
    #  ACCOUNT MANAGEMENT
    # ═══════════════════════════════════════════════════════════

    # ── Daftar semua akun ────────────────────────────────────
    elif data == "manage_accounts":
        accounts = state.config.get("accounts", [])
        if not accounts:
            text = "👤 *Kelola Akun*\n\nBelum ada akun. Tambahkan akun baru!"
        else:
            text = f"👤 *Kelola Akun* ({len(accounts)} akun)\n\nPilih akun untuk melihat/edit:"
        await query.edit_message_text(
            text, reply_markup=accounts_menu_keyboard(), parse_mode="Markdown"
        )

    # ── Tambah akun baru ─────────────────────────────────────
    elif data == "acc_add":
        ctx.user_data["awaiting"] = "acc_add_name"
        await query.edit_message_text(
            "➕ *Tambah Akun Baru*\n\n"
            "*Langkah 1/5:* Kirim nama label untuk akun ini.\n"
            "Contoh: `Akun VPS Promo`\n\n"
            "Kirim `batal` untuk membatalkan.",
            parse_mode="Markdown",
        )

    # ── View detail akun ─────────────────────────────────────
    elif data.startswith("acc_view_"):
        idx = int(data.replace("acc_view_", ""))
        accounts = state.config.get("accounts", [])
        if idx >= len(accounts):
            await query.edit_message_text("❌ Akun tidak ditemukan.", reply_markup=accounts_menu_keyboard())
            return
        acc = accounts[idx]
        token_preview = acc.get("token", "-")
        if len(token_preview) > 20:
            token_preview = token_preview[:10] + "..." + token_preview[-6:]
        proxy = acc.get("proxy") or "Tidak digunakan"
        channels = acc.get("channels", [])
        
        ch_list_str = []
        for c in channels:
            if isinstance(c, dict):
                c_id = c.get("id", "?")
                c_name = c.get("name", "")
                m = c.get("message", "").strip()
                display_c = f"{c_name} ({c_id})" if c_name else f"{c_id}"
                t = f"   • `{display_c}`" + (" (Pesan Khusus)" if m else "")
                ch_list_str.append(t)
            else:
                ch_list_str.append(f"   • `{c}`")
                
        ch_list = "\n".join(ch_list_str) if channels else "   (kosong)"
        msg_preview = acc.get("message", "-")
        if len(msg_preview) > 150:
            msg_preview = msg_preview[:150] + "..."

        # Check if active
        stats = state.stats.get(acc["name"], {})
        status = "🟢 Aktif" if stats.get("active") else "🔴 Tidak aktif"

        await query.edit_message_text(
            f"👤 *Detail Akun: {acc['name']}*\n\n"
            f"Status: {status}\n"
            f"🔑 Token: `{token_preview}`\n"
            f"🌐 Proxy: `{proxy}`\n\n"
            f"📺 Channels ({len(channels)}):\n{ch_list}\n\n"
            f"💬 Pesan:\n```\n{msg_preview}\n```",
            reply_markup=account_detail_keyboard(idx),
            parse_mode="Markdown",
        )

    # ── Ubah token ───────────────────────────────────────────
    elif data.startswith("acc_token_"):
        idx = int(data.replace("acc_token_", ""))
        ctx.user_data["awaiting"] = "acc_set_token"
        ctx.user_data["acc_idx"] = idx
        acc = state.config["accounts"][idx]
        await query.edit_message_text(
            f"🔑 *Ubah Token — {acc['name']}*\n\n"
            "Kirim Discord User Token baru.\n\n"
            "⚠️ Jangan share token ke siapapun!\n"
            "Kirim `batal` untuk membatalkan.",
            parse_mode="Markdown",
        )

    # ── Ubah nama ────────────────────────────────────────────
    elif data.startswith("acc_name_"):
        idx = int(data.replace("acc_name_", ""))
        ctx.user_data["awaiting"] = "acc_set_name"
        ctx.user_data["acc_idx"] = idx
        acc = state.config["accounts"][idx]
        await query.edit_message_text(
            f"✏️ *Ubah Nama — {acc['name']}*\n\n"
            "Kirim nama baru untuk akun ini.\n"
            "Kirim `batal` untuk membatalkan.",
            parse_mode="Markdown",
        )

    # ── Ubah proxy ───────────────────────────────────────────
    elif data.startswith("acc_proxy_"):
        idx = int(data.replace("acc_proxy_", ""))
        ctx.user_data["awaiting"] = "acc_set_proxy"
        ctx.user_data["acc_idx"] = idx
        acc = state.config["accounts"][idx]
        current_proxy = acc.get("proxy") or "Tidak ada"
        await query.edit_message_text(
            f"🌐 *Ubah Proxy — {acc['name']}*\n\n"
            f"Proxy saat ini: `{current_proxy}`\n\n"
            "Kirim proxy baru (format: `http://user:pass@host:port`)\n"
            "Kirim `hapus` untuk menghapus proxy\n"
            "Kirim `batal` untuk membatalkan.",
            parse_mode="Markdown",
        )

    # ── Ubah pesan ───────────────────────────────────────────
    elif data.startswith("acc_msg_"):
        idx = int(data.replace("acc_msg_", ""))
        ctx.user_data["awaiting"] = "acc_set_message"
        ctx.user_data["acc_idx"] = idx
        acc = state.config["accounts"][idx]
        current_msg = acc.get("message", "-")
        await query.edit_message_text(
            f"💬 *Ubah Pesan — {acc['name']}*\n\n"
            f"Pesan saat ini:\n```\n{current_msg}\n```\n\n"
            "Kirim pesan baru (bisa pakai spintax `{{opsi1|opsi2}}`).\n"
            "Kirim `batal` untuk membatalkan.",
            parse_mode="Markdown",
        )

    # ── Validasi token ───────────────────────────────────────
    elif data.startswith("acc_validate_"):
        idx = int(data.replace("acc_validate_", ""))
        acc = state.config["accounts"][idx]
        token = acc.get("token", "")
        proxy = acc.get("proxy")

        if not token or token.startswith("ISI_"):
            await query.edit_message_text(
                f"❌ Token belum diisi untuk *{acc['name']}*",
                reply_markup=account_detail_keyboard(idx),
                parse_mode="Markdown",
            )
            return

        # Tampilkan loading
        await query.edit_message_text(
            f"🔍 Memvalidasi token *{acc['name']}*...",
            parse_mode="Markdown",
        )

        result = await validate_discord_token(token, proxy)
        if result.get("valid"):
            await query.edit_message_text(
                f"✅ *Token Valid!*\n\n"
                f"👤 Username: `{result['username']}#{result['discriminator']}`\n"
                f"🆔 User ID: `{result['id']}`\n"
                f"📋 Akun: *{acc['name']}*",
                reply_markup=account_detail_keyboard(idx),
                parse_mode="Markdown",
            )
        else:
            err = result.get("error", f"HTTP {result.get('status_code', '?')}")
            await query.edit_message_text(
                f"❌ *Token Tidak Valid!*\n\n"
                f"Akun: *{acc['name']}*\n"
                f"Error: `{err}`",
                reply_markup=account_detail_keyboard(idx),
                parse_mode="Markdown",
            )

    # ── Hapus akun ───────────────────────────────────────────
    elif data.startswith("acc_delete_"):
        idx = int(data.replace("acc_delete_", ""))
        accounts = state.config.get("accounts", [])
        if idx >= len(accounts):
            await query.edit_message_text("❌ Akun tidak ditemukan.", reply_markup=accounts_menu_keyboard())
            return
        acc = accounts[idx]
        await query.edit_message_text(
            f"🗑 *Hapus Akun: {acc['name']}?*\n\n"
            "⚠️ Aksi ini tidak bisa dibatalkan!",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Ya, Hapus", callback_data=f"acc_confirm_del_{idx}"),
                    InlineKeyboardButton("❌ Batal", callback_data=f"acc_view_{idx}"),
                ]
            ]),
            parse_mode="Markdown",
        )

    elif data.startswith("acc_confirm_del_"):
        idx = int(data.replace("acc_confirm_del_", ""))
        accounts = state.config.get("accounts", [])
        if idx < len(accounts):
            deleted_name = accounts[idx]["name"]
            accounts.pop(idx)
            state.save_config()
            state.stats.pop(deleted_name, None)
            await query.edit_message_text(
                f"🗑 *Akun `{deleted_name}` berhasil dihapus!*",
                reply_markup=accounts_menu_keyboard(),
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text("❌ Akun tidak ditemukan.", reply_markup=accounts_menu_keyboard())

    # ═══════════════════════════════════════════════════════════
    #  CHANNEL MANAGEMENT
    # ═══════════════════════════════════════════════════════════

    elif data.startswith("acc_channels_"):
        idx = int(data.replace("acc_channels_", ""))
        accounts = state.config.get("accounts", [])
        if idx >= len(accounts):
            await query.edit_message_text("❌ Akun tidak ditemukan.", reply_markup=accounts_menu_keyboard())
            return
        acc = accounts[idx]
        ch_count = len(acc.get("channels", []))
        await query.edit_message_text(
            f"📺 *Channels — {acc['name']}*\n\n"
            f"Total: `{ch_count}` channel\n\n"
            "Tap channel untuk menghapus, atau tambah baru:",
            reply_markup=channels_keyboard(idx),
            parse_mode="Markdown",
        )

    elif data.startswith("ch_add_"):
        idx = int(data.replace("ch_add_", ""))
        ctx.user_data["awaiting"] = "ch_add"
        ctx.user_data["acc_idx"] = idx
        acc = state.config["accounts"][idx]
        await query.edit_message_text(
            f"➕ *Tambah Channel — {acc['name']}*\n\n"
            "Kirim Channel ID (angka).\n"
            "Bisa kirim beberapa sekaligus, pisahkan dengan koma atau baris baru.\n\n"
            "Contoh:\n`1234567890123`\natau\n`1234567890123, 9876543210987`\n\n"
            "Kirim `batal` untuk membatalkan.",
            parse_mode="Markdown",
        )

    elif data.startswith("ch_view_"):
        parts = data.replace("ch_view_", "").split("_")
        acc_idx = int(parts[0])
        ch_idx = int(parts[1])
        accounts = state.config.get("accounts", [])
        if acc_idx >= len(accounts):
            await query.edit_message_text("❌ Akun tidak ditemukan.", reply_markup=accounts_menu_keyboard())
            return
            
        acc = accounts[acc_idx]
        channels = acc.get("channels", [])
        if ch_idx >= len(channels):
            await query.edit_message_text("❌ Channel tidak ditemukan.", reply_markup=channels_keyboard(acc_idx))
            return
            
        ch_item = channels[ch_idx]
        if isinstance(ch_item, dict):
            ch_id = ch_item.get("id", "?")
            ch_name = ch_item.get("name", "")
            ch_msg = ch_item.get("message", "").strip()
            ch_delay = ch_item.get("delay")
        else:
            ch_id = ch_item
            ch_name = ""
            ch_msg = ""
            ch_delay = None
            
        display_name = f"*{ch_name}*\n`{ch_id}`" if ch_name else f"`{ch_id}`"
            
        if ch_msg:
            msg_preview = ch_msg
            if len(msg_preview) > 150: msg_preview = msg_preview[:150] + "..."
            status_msg = f"✅ *Pesan Khusus:*\n```\n{msg_preview}\n```\n"
        else:
            status_msg = f"ℹ️ Pesan: _default akun_\n"
            
        if ch_delay is not None:
            status_msg += f"⏱ *Delay Khusus:* `{ch_delay} menit`"
        else:
            status_msg += f"⏱ Delay: _default akun_"
            
        await query.edit_message_text(
            f"📺 *Detail Channel*\n"
            f"Channel: {display_name}\n"
            f"Akun: *{acc['name']}*\n\n"
            f"{status_msg}",
            reply_markup=channel_detail_keyboard(acc_idx, ch_idx),
            parse_mode="Markdown"
        )
        
    elif data.startswith("ch_name_"):
        parts = data.replace("ch_name_", "").split("_")
        acc_idx = int(parts[0])
        ch_idx = int(parts[1])
        ctx.user_data["awaiting"] = "ch_set_name"
        ctx.user_data["acc_idx"] = acc_idx
        ctx.user_data["ch_idx"] = ch_idx
        
        acc = state.config["accounts"][acc_idx]
        ch_item = acc["channels"][ch_idx]
        
        if isinstance(ch_item, dict):
            ch_id = ch_item.get("id", "?")
            current_name = ch_item.get("name", "")
        else:
            ch_id = ch_item
            current_name = ""
            
        name_disp = f"`{current_name}`" if current_name else "*(Tanpa Nama)*"
        
        await query.edit_message_text(
            f"✏️ *Ubah Nama Channel — {ch_id}*\n\n"
            f"Nama saat ini: {name_disp}\n\n"
            "Kirim nama baru untuk channel ini (misal: `Server Jual Beli`).\n"
            "Kirim `hapus` untuk menghapus nama.\n"
            "Kirim `batal` untuk membatalkan.",
            parse_mode="Markdown",
        )
        
    elif data.startswith("ch_delay_"):
        parts = data.replace("ch_delay_", "").split("_")
        acc_idx = int(parts[0])
        ch_idx = int(parts[1])
        ctx.user_data["awaiting"] = "ch_set_delay"
        ctx.user_data["acc_idx"] = acc_idx
        ctx.user_data["ch_idx"] = ch_idx
        
        acc = state.config["accounts"][acc_idx]
        ch_item = acc["channels"][ch_idx]
        
        if isinstance(ch_item, dict):
            ch_id = ch_item.get("id", "?")
            current_delay = ch_item.get("delay")
        else:
            ch_id = ch_item
            current_delay = None
            
        delay_disp = f"`{current_delay} menit`" if current_delay is not None else "*(Mengikuti waktu default)*"
        
        await query.edit_message_text(
            f"⏱ *Ubah Delay Channel — {ch_id}*\n\n"
            f"Waktu tunggu saat ini: {delay_disp}\n\n"
            "Kirim angka dalam satuan **menit** (misal: `120` untuk 2 jam).\n"
            "Kirim `hapus` untuk kembali mengikuti waktu tunggu default.\n"
            "Kirim `batal` untuk membatalkan.",
            parse_mode="Markdown",
        )
        
    elif data.startswith("ch_msg_"):
        parts = data.replace("ch_msg_", "").split("_")
        acc_idx = int(parts[0])
        ch_idx = int(parts[1])
        ctx.user_data["awaiting"] = "ch_set_message"
        ctx.user_data["acc_idx"] = acc_idx
        ctx.user_data["ch_idx"] = ch_idx
        
        acc = state.config["accounts"][acc_idx]
        ch_item = acc["channels"][ch_idx]
        
        if isinstance(ch_item, dict):
            ch_id = ch_item.get("id", "?")
            current_msg = ch_item.get("message", "").strip()
        else:
            ch_id = ch_item
            current_msg = ""
            
        msg_disp = f"```\n{current_msg}\n```" if current_msg else "*(Pesan default akun)*"
        
        await query.edit_message_text(
            f"💬 *Ubah Pesan Khusus Channel — {ch_id}*\n\n"
            f"Pesan saat ini:\n{msg_disp}\n\n"
            "Kirim pesan khusus untuk channel ini (bisa pakai spintax `{opsi1|opsi2}`).\n"
            "Kirim `hapus` untuk kembali menggunakan pesan default akun.\n"
            "Kirim `batal` untuk membatalkan.",
            parse_mode="Markdown",
        )

    elif data.startswith("ch_remove_"):
        parts = data.replace("ch_remove_", "").split("_")
        acc_idx = int(parts[0])
        ch_idx = int(parts[1])
        accounts = state.config.get("accounts", [])
        if acc_idx < len(accounts):
            channels = accounts[acc_idx].get("channels", [])
            if ch_idx < len(channels):
                removed = channels.pop(ch_idx)
                if isinstance(removed, dict):
                    rem_id = removed.get("id", "?")
                else:
                    rem_id = removed
                state.save_config()
                await query.edit_message_text(
                    f"🗑 Channel `{rem_id}` dihapus!",
                    reply_markup=channels_keyboard(acc_idx),
                    parse_mode="Markdown",
                )
                return
        await query.edit_message_text("❌ Channel tidak ditemukan.", reply_markup=accounts_menu_keyboard())


# ── Text Message Handler ─────────────────────────────────────
async def text_input_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    awaiting = ctx.user_data.get("awaiting")
    text = update.message.text.strip()

    if not awaiting:
        await update.message.reply_text("🤖 Gunakan /start untuk membuka menu.")
        return

    # ── Batal ────────────────────────────────────────────────
    if text.lower() == "batal":
        ctx.user_data.clear()
        await update.message.reply_text("❌ Dibatalkan.", reply_markup=main_menu_keyboard())
        return

    # ── Input Delay ──────────────────────────────────────────
    if awaiting == "delay":
        try:
            new_delay = int(text)
            if new_delay < 1:
                raise ValueError
            state.config["default_delay_minutes"] = new_delay
            state.save_config()
            ctx.user_data.clear()
            await update.message.reply_text(
                f"✅ *Delay diubah menjadi {new_delay} menit!*\n"
                f"(±{state.config.get('delay_variation_percent', 10)}% variasi)",
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown",
            )
        except ValueError:
            await update.message.reply_text("❌ Masukkan angka valid (min 1). Coba lagi atau kirim `batal`.")
        return

    # ═══════════════════════════════════════════════════════════
    #  ADD ACCOUNT WIZARD (5 langkah)
    # ═══════════════════════════════════════════════════════════

    # Langkah 1: Nama akun
    if awaiting == "acc_add_name":
        ctx.user_data["new_acc"] = {"name": text, "channels": [], "proxy": None, "message": "Hello!"}
        ctx.user_data["awaiting"] = "acc_add_token"
        await update.message.reply_text(
            f"✅ Nama: *{text}*\n\n"
            "*Langkah 2/5:* Kirim Discord User Token.\n\n"
            "⚠️ Token bisa didapat dari DevTools browser → Application → Local Storage.\n"
            "Kirim `batal` untuk membatalkan.",
            parse_mode="Markdown",
        )
        return

    # Langkah 2: Token
    if awaiting == "acc_add_token":
        ctx.user_data["new_acc"]["token"] = text
        ctx.user_data["awaiting"] = "acc_add_proxy"

        # Auto-validasi token
        await update.message.reply_text("🔍 Memvalidasi token...")
        result = await validate_discord_token(text)
        if result.get("valid"):
            await update.message.reply_text(
                f"✅ Token valid! User: `{result['username']}#{result['discriminator']}`\n\n"
                "*Langkah 3/5:* Kirim proxy (format: `http://user:pass@host:port`)\n"
                "Kirim `skip` jika tidak pakai proxy.\n"
                "Kirim `batal` untuk membatalkan.",
                parse_mode="Markdown",
            )
        else:
            err = result.get("error", f"HTTP {result.get('status_code', '?')}")
            await update.message.reply_text(
                f"⚠️ Token sepertinya tidak valid: `{err}`\n"
                "Token tetap disimpan, bisa diubah nanti.\n\n"
                "*Langkah 3/5:* Kirim proxy atau `skip`.\n"
                "Kirim `batal` untuk membatalkan.",
                parse_mode="Markdown",
            )
        return

    # Langkah 3: Proxy
    if awaiting == "acc_add_proxy":
        if text.lower() == "skip":
            ctx.user_data["new_acc"]["proxy"] = None
        else:
            ctx.user_data["new_acc"]["proxy"] = text
        ctx.user_data["awaiting"] = "acc_add_channels"
        proxy_info = ctx.user_data["new_acc"]["proxy"] or "Tidak digunakan"
        await update.message.reply_text(
            f"✅ Proxy: `{proxy_info}`\n\n"
            "*Langkah 4/5:* Kirim Channel ID tujuan.\n"
            "Bisa kirim beberapa, pisahkan dengan koma atau baris baru.\n\n"
            "Contoh:\n`1234567890123`\n`9876543210987, 1111222233334`\n\n"
            "Kirim `batal` untuk membatalkan.",
            parse_mode="Markdown",
        )
        return

    # Langkah 4: Channels
    if awaiting == "acc_add_channels":
        # Parse channel IDs (koma, spasi, atau newline separated)
        raw = text.replace(",", "\n").replace(" ", "\n")
        channel_ids = [ch.strip() for ch in raw.split("\n") if ch.strip()]
        
        # Simpan sebagai list dari dict
        channels = [{"id": ch_id, "name": "", "message": "", "delay": None} for ch_id in channel_ids]
        
        ctx.user_data["new_acc"]["channels"] = channels
        ctx.user_data["awaiting"] = "acc_add_message"
        ch_list = "\n".join([f"• `{c}`" for c in channel_ids])
        await update.message.reply_text(
            f"✅ {len(channels)} channel ditambahkan:\n{ch_list}\n\n"
            "*Langkah 5/5:* Kirim teks pesan promosi.\n"
            "Gunakan spintax: `{opsi1|opsi2|opsi3}`\n\n"
            "Contoh:\n`{Halo|Hi} gan, ready {VPS|RDP} murah!`\n\n"
            "Kirim `batal` untuk membatalkan.",
            parse_mode="Markdown",
        )
        return

    # Langkah 5: Pesan
    if awaiting == "acc_add_message":
        ctx.user_data["new_acc"]["message"] = text
        new_acc = ctx.user_data["new_acc"]

        # Simpan akun baru ke config
        if "accounts" not in state.config:
            state.config["accounts"] = []
        state.config["accounts"].append(new_acc)
        state.save_config()

        # Init stats
        state.stats[new_acc["name"]] = {
            "total_sent": 0, "total_failed": 0,
            "last_channel": "-", "last_time": "-", "active": False,
        }

        preview = parse_spintax(text)
        ctx.user_data.clear()
        await update.message.reply_text(
            f"🎉 *Akun Berhasil Ditambahkan!*\n\n"
            f"👤 Nama: *{new_acc['name']}*\n"
            f"🌐 Proxy: `{new_acc.get('proxy') or 'Tidak ada'}`\n"
            f"📺 Channels: `{len(new_acc['channels'])}`\n\n"
            f"💬 Preview pesan:\n```\n{preview}\n```",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown",
        )
        return

    # ═══════════════════════════════════════════════════════════
    #  EDIT ACCOUNT FIELDS
    # ═══════════════════════════════════════════════════════════

    idx = ctx.user_data.get("acc_idx", 0)
    accounts = state.config.get("accounts", [])

    if idx >= len(accounts):
        ctx.user_data.clear()
        await update.message.reply_text("❌ Akun tidak ditemukan.", reply_markup=main_menu_keyboard())
        return

    acc = accounts[idx]

    # ── Set Token ────────────────────────────────────────────
    if awaiting == "acc_set_token":
        old_name = acc["name"]
        acc["token"] = text
        state.save_config()

        # Auto-validasi
        await update.message.reply_text("🔍 Memvalidasi token baru...")
        result = await validate_discord_token(text, acc.get("proxy"))
        ctx.user_data.clear()

        if result.get("valid"):
            await update.message.reply_text(
                f"✅ *Token {old_name} berhasil diubah!*\n\n"
                f"👤 User: `{result['username']}#{result['discriminator']}`\n"
                f"🆔 ID: `{result['id']}`",
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown",
            )
        else:
            err = result.get("error", f"HTTP {result.get('status_code', '?')}")
            await update.message.reply_text(
                f"⚠️ Token disimpan tapi validasi gagal: `{err}`\n"
                "Token tetap tersimpan, cek kembali apakah sudah benar.",
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown",
            )
        return

    # ── Set Name ─────────────────────────────────────────────
    if awaiting == "acc_set_name":
        old_name = acc["name"]
        acc["name"] = text
        state.save_config()
        # Rename stats
        if old_name in state.stats:
            state.stats[text] = state.stats.pop(old_name)
        ctx.user_data.clear()
        await update.message.reply_text(
            f"✅ Nama akun diubah: `{old_name}` → `{text}`",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown",
        )
        return

    # ── Set Proxy ────────────────────────────────────────────
    if awaiting == "acc_set_proxy":
        if text.lower() == "hapus":
            acc["proxy"] = None
            msg = f"✅ Proxy untuk *{acc['name']}* dihapus."
        else:
            acc["proxy"] = text
            msg = f"✅ Proxy *{acc['name']}* diubah ke:\n`{text}`"
        state.save_config()
        ctx.user_data.clear()
        await update.message.reply_text(msg, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
        return

    # ── Set Message ──────────────────────────────────────────
    if awaiting == "acc_set_message":
        acc["message"] = text
        state.save_config()
        preview = parse_spintax(text)
        ctx.user_data.clear()
        await update.message.reply_text(
            f"✅ *Pesan untuk {acc['name']} berhasil diubah!*\n\n"
            f"Preview:\n```\n{preview}\n```",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown",
        )
        return

    # ── Add Channels ─────────────────────────────────────────
    if awaiting == "ch_add":
        raw = text.replace(",", "\n").replace(" ", "\n")
        new_channels = [ch.strip() for ch in raw.split("\n") if ch.strip()]
        if "channels" not in acc:
            acc["channels"] = []
            
        # Hindari duplikat
        existing_ids = []
        for c in acc["channels"]:
            if isinstance(c, dict): existing_ids.append(c.get("id"))
            else: existing_ids.append(c)
            
        added = []
        for ch in new_channels:
            if ch not in existing_ids:
                acc["channels"].append({"id": ch, "name": "", "message": "", "delay": None})
                added.append(ch)
                
        state.save_config()
        ctx.user_data.clear()
        if added:
            ch_list = "\n".join([f"• `{c}`" for c in added])
            await update.message.reply_text(
                f"✅ *{len(added)} channel ditambahkan ke {acc['name']}:*\n{ch_list}\n\n"
                f"Total channel sekarang: `{len(acc['channels'])}`",
                reply_markup=main_menu_keyboard(),
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                "ℹ️ Semua channel sudah ada (tidak ada duplikat ditambahkan).",
                reply_markup=main_menu_keyboard(),
            )
        return

    # ── Set Message Channel ──────────────────────────────────
    if awaiting == "ch_set_message":
        acc_idx = ctx.user_data.get("acc_idx", 0)
        ch_idx = ctx.user_data.get("ch_idx", 0)
        accounts = state.config.get("accounts", [])
        
        if acc_idx >= len(accounts):
            ctx.user_data.clear()
            await update.message.reply_text("❌ Akun tidak ditemukan.", reply_markup=main_menu_keyboard())
            return
            
        acc = accounts[acc_idx]
        if ch_idx >= len(acc.get("channels", [])):
            ctx.user_data.clear()
            await update.message.reply_text("❌ Channel tidak ditemukan.")
            return
            
        ch_item = acc["channels"][ch_idx]
        
        # Pastikan format dict
        if not isinstance(ch_item, dict):
            ch_item = {"id": ch_item, "name": "", "message": "", "delay": None}
            acc["channels"][ch_idx] = ch_item
            
        if text.lower() == "hapus":
            ch_item["message"] = ""
            msg_reply = f"✅ Pesan khusus untuk channel `{ch_item['id']}` dihapus.\n\nSekarang channel ini menggunakan pesan default akun."
        else:
            ch_item["message"] = text
            try:
                preview = parse_spintax(text)
            except Exception as e:
                preview = text
            msg_reply = f"✅ *Pesan khusus untuk channel `{ch_item['id']}` berhasil diatur!*\n\nPreview:\n```\n{preview}\n```"
            
        state.save_config()
        ctx.user_data.clear()
        
        await update.message.reply_text(
            msg_reply,
            reply_markup=channel_detail_keyboard(acc_idx, ch_idx),
            parse_mode="Markdown",
        )
        return

    # ── Set Name Channel ─────────────────────────────────────
    if awaiting == "ch_set_name":
        acc_idx = ctx.user_data.get("acc_idx", 0)
        ch_idx = ctx.user_data.get("ch_idx", 0)
        accounts = state.config.get("accounts", [])
        
        if acc_idx >= len(accounts):
            ctx.user_data.clear()
            await update.message.reply_text("❌ Akun tidak ditemukan.", reply_markup=main_menu_keyboard())
            return
            
        acc = accounts[acc_idx]
        if ch_idx >= len(acc.get("channels", [])):
            ctx.user_data.clear()
            await update.message.reply_text("❌ Channel tidak ditemukan.")
            return
            
        ch_item = acc["channels"][ch_idx]
        
        if not isinstance(ch_item, dict):
            ch_item = {"id": ch_item, "name": "", "message": "", "delay": None}
            acc["channels"][ch_idx] = ch_item
            
        if text.lower() == "hapus":
            ch_item["name"] = ""
            msg_reply = f"✅ Nama untuk channel `{ch_item['id']}` dihapus."
        else:
            ch_item["name"] = text
            msg_reply = f"✅ *Nama channel `{ch_item['id']}` berhasil diubah menjadi `{text}`!*"
            
        state.save_config()
        ctx.user_data.clear()
        
        await update.message.reply_text(
            msg_reply,
            reply_markup=channel_detail_keyboard(acc_idx, ch_idx),
            parse_mode="Markdown",
        )
        return

    # ── Set Delay Channel ─────────────────────────────────────
    if awaiting == "ch_set_delay":
        acc_idx = ctx.user_data.get("acc_idx", 0)
        ch_idx = ctx.user_data.get("ch_idx", 0)
        accounts = state.config.get("accounts", [])
        
        if acc_idx >= len(accounts):
            ctx.user_data.clear()
            await update.message.reply_text("❌ Akun tidak ditemukan.", reply_markup=main_menu_keyboard())
            return
            
        acc = accounts[acc_idx]
        if ch_idx >= len(acc.get("channels", [])):
            ctx.user_data.clear()
            await update.message.reply_text("❌ Channel tidak ditemukan.")
            return
            
        ch_item = acc["channels"][ch_idx]
        
        if not isinstance(ch_item, dict):
            ch_item = {"id": ch_item, "name": "", "message": "", "delay": None}
            acc["channels"][ch_idx] = ch_item
            
        if text.lower() == "hapus":
            ch_item["delay"] = None
            msg_reply = f"✅ Waktu tunggu untuk channel `{ch_item['id']}` dihapus.\nSekarang mengikuti pengaturan waktu default."
        else:
            try:
                delay_val = int(text)
                if delay_val < 1: raise ValueError
                ch_item["delay"] = delay_val
                msg_reply = f"✅ *Waktu tunggu channel `{ch_item['id']}` berhasil diubah menjadi `{delay_val} menit`!*"
            except ValueError:
                await update.message.reply_text("❌ Masukkan angka menit valid (min 1). Coba lagi atau kirim `batal`.")
                return
            
        state.save_config()
        ctx.user_data.clear()
        
        await update.message.reply_text(
            msg_reply,
            reply_markup=channel_detail_keyboard(acc_idx, ch_idx),
            parse_mode="Markdown",
        )
        return


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
    # Auto-restore: jika config.json tidak ada, coba restore dari backup
    if not CONFIG_PATH.exists():
        backups = sorted(BACKUP_DIR.glob("config_*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if BACKUP_DIR.exists() else []
        if backups:
            latest = backups[0]
            shutil.copy2(latest, CONFIG_PATH)
            logger.info("⚡ config.json tidak ditemukan — di-restore dari backup: %s", latest.name)
        else:
            logger.error("❌ config.json tidak ditemukan dan tidak ada backup!")
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
