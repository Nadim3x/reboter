#!/bin/bash
# ==============================================================================
# start.sh — Start the Telegram bot and dashboard together.
#
# Optional Cloudflare Quick Tunnel:
#   ./start.sh --tunnel
#   DASHBOARD_TUNNEL=1 ./start.sh
# The tunnel is intentionally opt-in: it publishes the dashboard to the
# internet, so the password must be configured before enabling it.
# ==============================================================================
set -euo pipefail
cd "$(dirname "$0")"

# Load .env before parsing flags so a saved DASHBOARD_TUNNEL setting can be
# overridden with --no-tunnel for one run.
if [ -f ".env" ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

TUNNEL_REQUESTED="${DASHBOARD_TUNNEL:-0}"
show_help() {
  cat <<'EOF'
Usage: ./start.sh [--tunnel|--no-tunnel]

Starts the Telegram bot and the password-protected dashboard.

  --tunnel       Start a Cloudflare Quick Tunnel and print its HTTPS URL.
                 Requires the cloudflared command to be installed.
  --no-tunnel    Start locally, overriding DASHBOARD_TUNNEL for this run.
  -h, --help     Show this help.

The tunnel is temporary and gets a new trycloudflare.com URL each time.
For a stable production hostname, use a named Cloudflare Tunnel instead.
EOF
}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --tunnel) TUNNEL_REQUESTED=1; shift ;;
    --no-tunnel) TUNNEL_REQUESTED=0; shift ;;
    -h|--help) show_help; exit 0 ;;
    *) echo "❌ Unknown option: $1 (try ./start.sh --help)" >&2; exit 2 ;;
  esac
done

if [ "$TUNNEL_REQUESTED" = "1" ] || [ "$TUNNEL_REQUESTED" = "true" ] || [ "$TUNNEL_REQUESTED" = "yes" ]; then
  TUNNEL_REQUESTED=1
else
  TUNNEL_REQUESTED=0
fi

DASHBOARD_HOST="${DASHBOARD_HOST:-0.0.0.0}"
DASHBOARD_PORT="${DASHBOARD_PORT:-5000}"
if ! [[ "$DASHBOARD_PORT" =~ ^[0-9]+$ ]] || [ "$DASHBOARD_PORT" -lt 1 ] || [ "$DASHBOARD_PORT" -gt 65535 ]; then
  echo "❌ DASHBOARD_PORT must be a number between 1 and 65535 (got: $DASHBOARD_PORT)" >&2
  exit 2
fi

if [ "$TUNNEL_REQUESTED" -eq 1 ] && [ -z "${DASHBOARD_PASSWORD:-}" ]; then
  echo "❌ Refusing to start a public tunnel without DASHBOARD_PASSWORD configured." >&2
  echo "   Add credentials to .env or run ./install.sh first." >&2
  exit 1
fi
if [ "$TUNNEL_REQUESTED" -eq 1 ] && ! command -v cloudflared >/dev/null 2>&1; then
  echo "❌ --tunnel requires cloudflared, but it was not found in PATH." >&2
  echo "   Install it from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/" >&2
  exit 1
fi

# The dashboard reads proxy settings when its workers import the app, so set
# this before starting it rather than after the tunnel process is launched.
if [ "$TUNNEL_REQUESTED" -eq 1 ]; then
  export DASHBOARD_TRUST_PROXY=1
  export DASHBOARD_COOKIE_SECURE=1
fi

echo "🚀 Starting AutoRepost Pipeline..."

mkdir -p downloads thumbnails logs

# Pick the interpreter: virtualenv first, then system python3.
PYTHON_BIN=""
if [ -x ".venv/bin/python" ]; then
  PYTHON_BIN=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  echo "❌ python3 not found! Run ./install.sh (or install Python 3.10+)" >&2
  exit 1
fi
echo "🐍 Using interpreter: $PYTHON_BIN"

if [ ! -f "config.json" ]; then
  echo "⚠️  config.json not found!"
  echo "Run: cp config.example.json config.json  (or ./install.sh)"
  exit 1
fi

if [ -z "${DASHBOARD_PASSWORD:-}" ]; then
  echo "⚠️  DASHBOARD_PASSWORD is not set — nobody will be able to log in to the dashboard."
  echo "   Add DASHBOARD_USER / DASHBOARD_PASSWORD to .env (or re-run ./install.sh) and restart."
fi

# Ensure dependencies are importable.
echo "🔍 Checking dependencies..."
"$PYTHON_BIN" -c "import telegram, yt_dlp, flask, gunicorn, requests" 2>/dev/null || {
  echo "⚠️  Some dependencies missing, installing..."
  "$PYTHON_BIN" -m pip install -r requirements.txt \
    || "$PYTHON_BIN" -m pip install --user -r requirements.txt
}

BOT_PID=""
DASH_PID=""
TUNNEL_PID=""
TUNNEL_LOG=""
cleanup() {
  local status=$?
  trap - EXIT INT TERM
  echo ""
  echo "🛑 Stopping services..."
  [ -n "$TUNNEL_PID" ] && kill "$TUNNEL_PID" 2>/dev/null || true
  [ -n "$DASH_PID" ] && kill "$DASH_PID" 2>/dev/null || true
  [ -n "$BOT_PID" ] && kill "$BOT_PID" 2>/dev/null || true
  [ -n "$TUNNEL_PID" ] && wait "$TUNNEL_PID" 2>/dev/null || true
  [ -n "$DASH_PID" ] && wait "$DASH_PID" 2>/dev/null || true
  [ -n "$BOT_PID" ] && wait "$BOT_PID" 2>/dev/null || true
  [ -n "$TUNNEL_LOG" ] && rm -f "$TUNNEL_LOG"
  echo "Stopped."
  exit "$status"
}
trap cleanup EXIT INT TERM

# Start bot and report its real state.
echo "▶ Starting Telegram bot..."
"$PYTHON_BIN" bot.py &
BOT_PID=$!
sleep 2
if kill -0 "$BOT_PID" 2>/dev/null; then
  BOT_STATUS="running (PID $BOT_PID)"
else
  BOT_STATUS="exited — check the output above (bad token? network?)"
fi

# Gunicorn is used when installed; the Flask server remains a development-only
# fallback so a partially installed checkout still gives a useful error/log.
echo "▶ Starting web dashboard on http://${DASHBOARD_HOST}:${DASHBOARD_PORT} ..."
if "$PYTHON_BIN" -c "import gunicorn" >/dev/null 2>&1; then
  WORKERS="${DASHBOARD_WORKERS:-2}"
  "$PYTHON_BIN" -m gunicorn \
    --workers "$WORKERS" \
    --bind "${DASHBOARD_HOST}:${DASHBOARD_PORT}" \
    --access-logfile - \
    --error-logfile - \
    --chdir "$PWD" \
    dashboard.app:app &
else
  echo "⚠️  gunicorn is not installed; using Flask's fallback server. Re-run install.sh for production serving."
  "$PYTHON_BIN" dashboard/app.py &
fi
DASH_PID=$!

# Wait briefly for the dashboard's public liveness endpoint.
DASH_URL="http://127.0.0.1:${DASHBOARD_PORT}/healthz"
DASH_READY=0
for _ in $(seq 1 20); do
  if command -v curl >/dev/null 2>&1 && curl -fsS -m 2 "$DASH_URL" >/dev/null 2>&1; then
    DASH_READY=1
    break
  fi
  if ! kill -0 "$DASH_PID" 2>/dev/null; then
    echo "❌ Dashboard exited before it became ready." >&2
    exit 1
  fi
  sleep 1
done
if [ "$DASH_READY" -ne 1 ]; then
  echo "⚠️  Dashboard has not answered health checks yet; continuing to show service status."
fi

PUBLIC_URL=""
if [ "$TUNNEL_REQUESTED" -eq 1 ]; then
  # A Quick Tunnel terminates TLS at Cloudflare. Trusting its forwarded headers
  # is safe here because cloudflared connects locally to this process.
  TUNNEL_LOG="$(mktemp "${TMPDIR:-/tmp}/autorepost-cloudflared.XXXXXX")"
  echo "▶ Starting Cloudflare Quick Tunnel..."
  cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:${DASHBOARD_PORT}" > >(tee "$TUNNEL_LOG") 2>&1 &
  TUNNEL_PID=$!

  # cloudflared normally prints the random HTTPS URL within a few seconds.
  for _ in $(seq 1 60); do
    if [ -s "$TUNNEL_LOG" ]; then
      PUBLIC_URL="$(grep -Eo 'https://[-a-zA-Z0-9]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -n1 || true)"
      [ -n "$PUBLIC_URL" ] && break
    fi
    if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
      break
    fi
    sleep 0.25
done
  if [ -n "$PUBLIC_URL" ]; then
    echo ""
    echo "🔗 Public HTTPS dashboard: $PUBLIC_URL"
    echo "   Open this URL in your browser and use the dashboard credentials from .env."
  else
    echo "⚠️  Cloudflare tunnel is running, but its public URL has not appeared yet."
    echo "   Watch the cloudflared output above; it will print the URL when ready."
  fi
fi

echo ""
echo "✅ AutoRepost Pipeline is up!"
echo "📱 Bot: $BOT_STATUS"
echo "🌐 Local dashboard: http://localhost:${DASHBOARD_PORT} (PID $DASH_PID)"
if [ "$TUNNEL_REQUESTED" -eq 1 ] && [ -n "$PUBLIC_URL" ]; then
  echo "☁️  Cloudflare URL: $PUBLIC_URL"
fi
echo "🔒 Dashboard login: ${DASHBOARD_USER:-admin} (password from .env)"
echo ""
echo "📋 Logs: tail -f logs/pipeline.log"
echo "🛑 Press Ctrl+C to stop all services"
echo ""

# Wait until one of the long-running services exits; the EXIT trap cleans up
# the other processes and any tunnel.
wait
