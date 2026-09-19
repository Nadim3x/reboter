# 🎬 AutoRepost Pipeline

An automated video reposting pipeline that receives video links via Telegram, downloads them using `yt-dlp`, and reposts them to Instagram & TikTok via Zernio API. Includes a password-protected web dashboard for managing settings, thumbnails, captions, and logs.

## Features

- 📱 **Telegram Bot** — Send any video link and it auto-reposts
- ⬇️ **Universal Downloader** — Supports YouTube, Instagram, TikTok, Facebook Reels, and 1000+ sites via yt-dlp
- 📝 **Smart Captions** — Extracts original caption or falls back to a random caption pool
- 🖼️ **Thumbnail Management** — Default thumbnail applied to every post, managed via dashboard
- 🚀 **Multi-Platform Upload** — Posts to Instagram & TikTok via Zernio API
- 🌐 **Web Dashboard** — Manage tokens, thumbnails, captions, and view logs at `http://localhost:5000`
- ☁️ **Optional Cloudflare Tunnel** — Publish the local dashboard through a temporary HTTPS URL with `./start.sh --tunnel`
- 🔒 **Dashboard Login** — Password protected (login page + HTTP Basic auth) so it can be exposed remotely; failed logins are logged
- 🛠️ **One-Command Install** — `./install.sh` handles deps, virtualenv, config and an optional systemd service
- 🩺 **Health Endpoint** — `GET /healthz` is public and always returns only `{"status":"ok","auth_required":true}`
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

### One command (recommended)

```bash
git clone https://github.com/Nadim3x/reboter.git && cd reboter && ./install.sh
```

The installer is idempotent (safe to re-run) and does everything:

1. Installs system dependencies — `ffmpeg` + Python 3.10+ — via `apk` / `apt` / `dnf` / `pacman` / `zypper` / `brew`
2. Creates a virtualenv in `.venv/` and installs `requirements.txt`
3. Creates `downloads/`, `thumbnails/`, `logs/` and seeds `config.json` from the example
4. Prompts for your Telegram token and Zernio API key (writes them into `config.json`)
5. Verifies the install (imports + syntax check on every module)
6. Optionally installs a **systemd service** so the pipeline survives reboots

Then start it:

```bash
./start.sh
```

**Non-interactive / automation:**

```bash
./install.sh -n --telegram-token "123:ABC..." --zernio-key "zern_..." --service --start
```

| Flag | Purpose |
|------|---------|
| `-d, --dir DIR` | Install/clone directory (default: the checkout you ran it from) |
| `-t, --telegram-token TOK` | Telegram bot token (or env `TELEGRAM_TOKEN`) |
| `-z, --zernio-key KEY` | Zernio API key (or env `ZERNIO_API_KEY`) |
| `-n, --non-interactive` | Never prompt (for CI/containers) |
| `--service` | Install + enable a systemd service (`systemctl status autorepost`) |
| `--start` | Run `./start.sh` when the install finishes |
| `--skip-system-deps` | Don't touch the OS package manager |
| `--skip-venv` | Install with `pip --user` instead of a virtualenv |
| `--skip-update` | Don't `git pull` an existing checkout |
| `--repo-url URL` | Remote to clone when run outside a checkout |
| `--dry-run` | Show what would happen, change nothing |

Full flag list: `./install.sh --help`. Uninstall: `./uninstall.sh` (add `--purge` to also delete config, thumbnails and logs).

> Running from a clone is recommended. The `curl -fsSL <raw-install-url> | bash` form works too — it clones the repo to `~/AutoRepost-Pipeline` — but only for public repos.

### Manual install

```bash
git clone <your-repo-url>
cd AutoRepost-Pipeline

python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # or: pip install -r requirements.txt

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
  "zernio_api_base_url": "https://api.zernio.com",
  "default_thumbnail": "thumbnails/default.jpg",
  "post_to": ["instagram", "tiktok"],
  "captions": ["Your captions..."]
}
```

- Get **Telegram Bot Token** from [@BotFather](https://t.me/BotFather) on Telegram (`/newbot`)
- Get **Zernio API Key** from your Zernio dashboard
- Set the **Zernio API Base URL** in the dashboard Settings page if your account uses a different API host. The uploader uses `/v1/upload` by default.

> ⚠️ `config.json` is gitignored — your tokens will never be pushed to GitHub.

3. (Optional) Add a default thumbnail image to `thumbnails/default.jpg` or upload via dashboard later.

## How to Run

Use the start script (starts both bot and dashboard):

```bash
./start.sh
```

`start.sh` auto-detects `.venv/`, loads `.env` if present, and reports whether the bot
process actually stayed alive — so a bad token or offline network is obvious immediately.

### Configuration via `.env`

`install.sh` writes `.env` (mode 600) with the dashboard login and, if you chose one,
a non-default port. Any of these are picked up by both the dashboard and `start.sh`
(see `.env.example`; values with spaces or shell characters go in single quotes):

```bash
DASHBOARD_HOST=0.0.0.0          # bind address for the Flask dashboard
DASHBOARD_PORT=5000             # port (also used for previews/proxies)
DASHBOARD_TUNNEL=0               # optional: set to 1 to start a Cloudflare Quick Tunnel
DASHBOARD_USER=admin            # dashboard login
DASHBOARD_PASSWORD='s3cret'     # required — without it nobody can log in
DASHBOARD_SECRET_KEY=...        # signs login cookies (installer generates it)
DASHBOARD_SESSION_HOURS=12      # optional: how long a browser login lasts
DASHBOARD_TRUST_PROXY=1         # optional: behind nginx/Caddy/Cloudflare Tunnel
DASHBOARD_COOKIE_SECURE=1       # optional: cookie only over HTTPS
```

### Dashboard login & remote access

Every dashboard page requires a login; `/healthz` is the only public route.

- **Browser** — you are sent to `/login`. A correct password sets an HMAC-signed,
  HttpOnly session cookie (12 h by default). *Logout* is in the top bar.
- **Scripts / uptime checks** — send HTTP Basic auth instead:
  `curl -u admin:s3cret http://host:5000/healthz`
- **Failed logins** are written to `logs/pipeline.log` (with the client IP), e.g.
  `2026-09-19 12:00:00 | WARNING | Dashboard login FAILED | user 'admin' from 203.0.113.9 via form`
  — handy for fail2ban.
- **Changing the password** (edit `.env`, restart) invalidates all existing logins.
- **Forgot the password?** It is stored in plain text in `.env`; re-run `./install.sh -p NEWPASS`
  (or edit the file) and restart.

Credentials are never committed: `.env` is gitignored, and `config.json` does not hold them.
If you expose the dashboard on the internet, put it behind HTTPS (reverse proxy or a
tunnel) and set `DASHBOARD_TRUST_PROXY=1` — Basic auth and cookies are only as private as
the connection they travel over.

#### Optional Cloudflare Quick Tunnel

To publish the local dashboard through Cloudflare over HTTPS, install
[`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
and run:

```bash
./start.sh --tunnel
```

The terminal prints both the local address and a line like:

```text
🔗 Public HTTPS dashboard: https://random-words.trycloudflare.com
```

Open that HTTPS URL and sign in with the credentials in `.env`. The tunnel is
**opt-in**, requires `DASHBOARD_PASSWORD`, and is stopped when `start.sh` stops.
When launched this way, `start.sh` enables trusted proxy headers and secure cookies automatically.
A Quick Tunnel receives a new random URL on every start and is intended for
demos/testing. For a stable production domain, configure a named Cloudflare
Tunnel and point it at `http://127.0.0.1:5000` instead.

### 24/7 with systemd

```bash
./install.sh --service          # installs, enables and starts autorepost.service
systemctl status autorepost     # check
journalctl -u autorepost -f     # follow logs
```

Without root, the installer falls back to a user service (`systemctl --user`) and
reminds you to run `loginctl enable-linger $USER`.

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
├── install.sh             # One-command installer (deps, venv, config, systemd)
├── uninstall.sh           # Removes service + venv (--purge also deletes data)
├── start.sh               # Start both bot & dashboard
├── .env                   # Dashboard login + host/port (gitignored, written by install.sh)
├── .env.example           # Template for .env
├── downloads/             # Temp video storage (gitignored)
├── thumbnails/            # Thumbnail images (gitignored *.jpg/*.png)
├── logs/                  # Pipeline logs (gitignored)
└── dashboard/
    ├── app.py             # Flask dashboard
    └── templates/
        ├── base.html
        ├── login.html
        ├── index.html
        ├── settings.html
        ├── thumbnails.html
        ├── captions.html
        └── logs.html
```

## Dashboard Routes

All routes require a login (session cookie or HTTP Basic auth) except `/healthz`, `/login` and `/logout`.

| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Dashboard home with recent activity |
| `/login` | GET/POST | Login page (sets the session cookie) |
| `/logout` | POST | Clear the session cookie |
| `/healthz` | GET | Public liveness check: always returns only `{"status":"ok","auth_required":true}` |
| `/settings` | GET/POST | Manage Telegram token, Zernio API key & API base URL |
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

- [ ] Verify the upload path, headers, and multipart field names match the Zernio API spec (currently `video`, `thumbnail`, `caption`, `platform`)
- [ ] Add rate limiting for Telegram bot if needed
- [ ] Add support for scheduling posts via Zernio if API supports it

## License

MIT License - feel free to use and modify.

## Troubleshooting

**`config.json not found`** — Run `cp config.example.json config.json` and fill tokens.

**`Invalid or unsupported URL`** — The URL may be private, deleted, or yt-dlp doesn't support it yet. Update yt-dlp: `pip install -U yt-dlp`.

**`Video too large`** — Max 100MB. Try a shorter video.

**Dashboard not loading images** — Ensure thumbnails folder exists and Flask has read permission.

**Bot not responding** — Check `logs/pipeline.log` and verify the Telegram token is correct. The public `/healthz` endpoint only confirms that the dashboard is alive; inspect the dashboard overview or logs for pipeline state.

**API upload errors** — Open Settings in the dashboard and verify the Zernio API key and base URL. A host-only base URL receives `/v1/upload` automatically; a complete URL ending in `/upload` is used as entered.

**Dashboard says "login is not configured" / can't log in** — `DASHBOARD_PASSWORD` is missing from `.env`. Run `./install.sh` again (it prompts, or generates one) or add `DASHBOARD_USER=…` / `DASHBOARD_PASSWORD='…'` yourself, then restart. Failed attempts are listed in `logs/pipeline.log`.

**Dashboard port already in use** — Set another port: `DASHBOARD_PORT=5050 ./start.sh` (the installer can also write it to `.env`). The optional tunnel follows the selected port: `./start.sh --tunnel`.

**Cloudflare URL does not appear** — Install `cloudflared`, confirm it is available in `PATH`, and run `./start.sh --tunnel`. A Quick Tunnel needs outbound internet access and prints a new random URL each time.

**Service won't start** — `journalctl -u autorepost -n 50 --no-pager`. Most often it is a missing token in `config.json`.

**Reinstalling / weird environment** — Re-run `./install.sh` (it is idempotent) or wipe the venv with `./uninstall.sh`.
