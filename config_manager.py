import asyncio
import json
import logging
import random
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.json"
BACKUP_DIR = Path(__file__).parent / "backups"
MAX_BACKUPS = 5

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
                    channels[i] = {"id": ch, "name": "", "message": "", "delay": None}
                    migrated = True
                elif isinstance(ch, dict):
                    if "name" not in ch:
                        ch["name"] = ""
                        migrated = True
                    if "delay" not in ch:
                        ch["delay"] = None
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


# Global instance
state = PromoState()
