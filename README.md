# DC Auto Promotion Bot

Bot promosi otomatis Discord berbasis Python dengan antarmuka kendali jarak jauh (UI) melalui Telegram Bot.

---

## 📌 Penjelasan Struktur Konfigurasi & Perubahan Kode

Untuk menjaga keamanan token akun (Telegram Bot Token & Discord User Token) agar tidak terblokir oleh GitHub Secret Scanning / Push Protection, konfigurasi diatur sebagai berikut:

### 1. File Konfigurasi
- **`config.example.json` / `config.exml.json`**: File contoh/template konfigurasi yang aman di-upload ke GitHub (tanpa token asli).
- **`config.json`**: File konfigurasi utama yang dipakai bot saat berjalan lokal. File ini otomatis **diabaikan oleh Git** (`.gitignore`).
- **`backups/`**: Folder backup konfigurasi lokal, juga otomatis **diabaikan oleh Git**.

### 2. Logika Fallback di `main.py`
Fungsi `main()` di [main.py](main.py) telah dimodifikasi dengan mekanisme *auto-fallback*:
1. Mengecek apakah `config.json` ada.
2. Jika tidak ada, bot mencari file backup terbaru di `backups/`.
3. Jika folder backup kosong (misal saat baru di-clone dari GitHub), bot otomatis menyalin `config.example.json` (atau `config.exml.json`) menjadi `config.json` baru.
4. Pengguna cukup mengisi token di `config.json` tersebut dan menjalankan bot.

---

## 🚀 Cara Menjalankan Bot

1. **Persiapkan Environment & Dependensi**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Siapkan Konfigurasi**:
   Jika `config.json` belum ada, salin dari template:
   ```bash
   cp config.example.json config.json
   ```
   Lalu buka `config.json` dan isi data:
   - `telegram_bot_token`: Token bot Telegram Anda dari @BotFather.
   - `telegram_admin_id`: ID akun Telegram Anda sebagai admin bot.
   - `accounts`: Daftar akun Discord, token Discord, daftar channel, delay, dan pesan promo.

3. **Jalankan Aplikasi**:
   ```bash
   python main.py
   ```
   Buka Telegram Anda, cari bot Anda, lalu kirimkan perintah `/start` untuk membuka Control Panel.

---

## 🤖 Catatan untuk Agent AI / Developer Selanjutnya
- **Arsitektur Modular**:
  - `main.py`: Entry point aplikasi dan inisialisasi state/bot.
  - `config_manager.py`: Manajemen baca/tulis `config.json`, backup otomatis, dan state running.
  - `discord_client.py`: Worker task pengirim pesan berkala ke Discord dengan dukungan spintax, random delay, dan webhook/channel handling.
  - `telegram_ui.py`: Handler command dan inline keyboard Telegram untuk mengontrol start/stop bot, atur delay, akun, dan channel.
  - `utils.py`: Fungsi pembantu (spintax parser, text cleaner, format durasi).
- **Keamanan Token**: Jangan pernah menghapus `config.json` dan `backups/` dari file `.gitignore`. Selalu gunakan `config.example.json` untuk menunjukkan skema konfigurasi baru ke repositori publik.
