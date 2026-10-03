# 🎵 Unstream Telegram Music Bot (24/7 GitHub Actions Runner)

> Download songs, albums, and playlists from **Spotify, Deezer, Apple Music, YouTube & SoundCloud** as tagged MP3 files with album artwork and lyrics. Runs 24/7 perpetually on **GitHub Actions** with 10Gbps unmetered network!

Powered by the core engine of [Unstream](https://github.com/amiralibg/unstream).

---

## ⚡ Highlights

- 🟢 **Spotify:** Tracks, albums, and playlists without premium accounts or API keys.
- 🍎 **Apple Music:** Complete catalog resolution via iTunes Search API.
- 🟣 **Deezer:** Pristine studio metadata and cover art.
- 🔴 **YouTube & YT Music:** Full audio extraction for videos and playlists.
- 🟠 **SoundCloud:** Tracks and sets.
- 🏷️ **ID3v2 Tagging:** Artist, Album, Title, Year, Track number embedded.
- 🖼️ **HD Album Art:** Embedded into the MP3 container & sent as Telegram thumbnail.
- 📝 **Lyrics:** Synced (LRC) & plain lyrics fetched from LRCLIB/Genius and embedded into ID3 tags.
- 🔍 **Multi-Catalog Search:** Search across all providers simultaneously with interactive inline buttons.
- 🔒 **Admin Lock:** Strict admin-only access control (`ADMIN_CHAT_ID`).
- 🔄 **Perpetual 24/7 Uptime:** Auto-retriggering workflow runner loop on GitHub Actions.

---

## 🚀 Quick Setup

### 1. Create a Telegram Bot
1. Open [@BotFather](https://t.me/BotFather) on Telegram.
2. Send `/newbot` and get your `HTTP API Token`.

### 2. Configure GitHub Secrets
In your repository on GitHub:
1. Go to **Settings** > **Secrets and variables** > **Actions**.
2. Add a new secret:
   - Name: `TELEGRAM_BOT_TOKEN`
   - Secret: Your bot token from BotFather.
3. (Optional) Set `ADMIN_CHAT_ID` if different from default (`8874504954`).

### 3. Start the 24/7 Runner
1. Go to the **Actions** tab.
2. Select **🎵 Unstream Music Bot (Live 24/7 Runner)**.
3. Click **Run workflow**.
4. The bot will notify you on Telegram once live!

---

## 📜 License
MIT License. Inspired by [amiralibg/unstream](https://github.com/amiralibg/unstream).
