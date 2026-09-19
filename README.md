# 🎬 AutoRepost Pipeline

An automated video reposting pipeline that receives video links via Telegram, downloads them using `yt-dlp`, and reposts them to Instagram & TikTok via Zernio API. Includes a local web dashboard for managing settings, thumbnails, captions, and logs.

## Features

- 📱 **Telegram Bot** — Send any video link and it auto-reposts
- ⬇️ **Universal Downloader** — Supports YouTube, Instagram, TikTok, Facebook Reels, and 1000+ sites via yt-dlp
- 📝 **Smart Captions** — Extracts original caption or falls back to a random caption pool
- 🖼️ **Thumbnail Management** — Default thumbnail applied to every post, managed via dashboard
- 🚀 **Multi-Platform Upload** — Posts to Instagram & TikTok via Zernio API
- 🌐 **Web Dashboard** — Manage tokens, thumbnails, captions, and view logs at `http://localhost:5000`
- 📜 **Logging** — Full pipeline logging to `logs/pipeline.log`

## Requirements

- Python 3.11+
- Alpine Linux (or any Linux with Python 3.11)
- `ffmpeg` (required by yt-dlp for merging video/audio)

Install ffmpeg on Alpine:

```bash
apk add ffmpeg
```

On Ubuntu/Debian:

```bash
sudo apt update && sudo apt install ffmpeg
```

## Installation

```bash
# Clone the repo
git clone <your-repo-url>
cd AutoRepost-Pipeline

# Install Python dependencies
pip install -r requirements.txt

# Create required directories (if not present)
mkdir -p downloads thumbnails logs
```

## Setup

1. Copy the example config:

```bash
cp config.example.json config.json
```

2. Edit `config.json` and fill in your tokens:

```json
{
  "telegram_token": "123456:ABC-your-telegram-bot-token",
  "zernio_api_key": "your_zernio_api_key_here",
  "default_thumbnail": "thumbnails/default.jpg",
  "post_to": ["instagram", "tiktok"],
  "captions": ["Your captions..."]
}
```

- Get **Telegram Bot Token** from [@BotFather](https://t.me/BotFather) on Telegram (`/newbot`)
- Get **Zernio API Key** from your Zernio dashboard

> ⚠️ `config.json` is gitignored — your tokens will never be pushed to GitHub.

3. (Optional) Add a default thumbnail image to `thumbnails/default.jpg` or upload via dashboard later.

## How to Run

Use the start script (starts both bot and dashboard):

```bash
bash start.sh
```

Or run manually in two terminals:

```bash
# Terminal 1 - Telegram bot
python3 bot.py

# Terminal 2 - Web dashboard
python3 dashboard/app.py
```

- Bot runs and listens for Telegram messages
- Dashboard available at: **http://localhost:5000**

## How to Add Thumbnails

**Via Dashboard (recommended):**

1. Open http://localhost:5000/thumbnails
2. Upload a JPG/PNG/WEBP image
3. Click "Set as Default" on the image you want as default

**Manually:**

```bash
cp /path/to/image.jpg thumbnails/default.jpg
```

Then update `config.json` -> `default_thumbnail` to point to your file.

Supported formats: `.jpg`, `.jpeg`, `.png`, `.webp`

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and usage instructions |
| `/status` | Check if pipeline is running |
| `/captions` | List all captions in the pool |
| `/addcaption [text]` | Add a new caption to the pool |
| `/thumbnail` | Show current default thumbnail filename |
| `/help` | List all commands with descriptions |

**Usage:**

- Simply send any video URL (YouTube, TikTok, Instagram, Facebook, etc.) and the bot will download and repost automatically.
- URL detection works for any message containing `http://` or `https://`.

## Project Structure

```
.
├── bot.py                 # Telegram bot (async)
├── downloader.py          # yt-dlp downloader
├── caption.py             # Caption selector & config helpers
├── uploader.py            # Zernio API uploader
├── config.json            # Your secrets (gitignored)
├── config.example.json    # Template config
├── requirements.txt
├── start.sh               # Start both bot & dashboard
├── downloads/             # Temp video storage (gitignored)
├── thumbnails/            # Thumbnail images (gitignored *.jpg/*.png)
├── logs/                  # Pipeline logs (gitignored)
└── dashboard/
    ├── app.py             # Flask dashboard
    └── templates/
        ├── base.html
        ├── index.html
        ├── settings.html
        ├── thumbnails.html
        ├── captions.html
        └── logs.html
```

## Dashboard Routes

| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Dashboard home with recent activity |
| `/settings` | GET/POST | Manage Telegram token & Zernio API key |
| `/thumbnails` | GET | List thumbnails & current default |
| `/thumbnails/upload` | POST | Upload new thumbnail |
| `/thumbnails/set` | POST | Set default thumbnail |
| `/captions` | GET | List all captions |
| `/captions/add` | POST | Add new caption |
| `/captions/delete` | POST | Delete caption by index |
| `/logs` | GET | View last 100 log entries |

## Logging

All pipeline actions are logged to `logs/pipeline.log` with format:

```
timestamp | url | caption_used | success/fail
```

View logs via dashboard at `/logs` or:

```bash
tail -f logs/pipeline.log
```

## TODOs

- [ ] Replace Zernio API endpoint in `uploader.py` with real endpoint from Zernio dashboard
- [ ] Verify multipart field names match Zernio API spec (currently `video`, `thumbnail`, `caption`, `platform`)
- [ ] Add rate limiting for Telegram bot if needed
- [ ] Add support for scheduling posts via Zernio if API supports it

## License

MIT License - feel free to use and modify.

## Troubleshooting

**`config.json not found`** — Run `cp config.example.json config.json` and fill tokens.

**`Invalid or unsupported URL`** — The URL may be private, deleted, or yt-dlp doesn't support it yet. Update yt-dlp: `pip install -U yt-dlp`.

**`Video too large`** — Max 100MB. Try a shorter video.

**Dashboard not loading images** — Ensure thumbnails folder exists and Flask has read permission.

**Bot not responding** — Check `logs/pipeline.log` and verify Telegram token is correct.
