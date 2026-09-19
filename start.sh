#!/bin/bash
# ==============================================================================
# start.sh — Start the Telegram bot and the web dashboard together.
#
# If install.sh created a virtualenv (.venv), it is used automatically.
# Settings from .env (DASHBOARD_HOST / DASHBOARD_PORT / DASHBOARD_USER /
# DASHBOARD_PASSWORD) are loaded if present.
# ==============================================================================
set -e
cd "$(dirname "$0")"

echo "🚀 Starting AutoRepost Pipeline..."

# Load .env if present (dashboard host/port + login credentials)
if [ -f ".env" ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

# Create required directories
mkdir -p downloads thumbnails logs

# Pick the interpreter: virtualenv first, then system python3
PYTHON_BIN=""
if [ -x ".venv/bin/python" ]; then
  PYTHON_BIN=".venv/bin/python"
elif command -v python3 &> /dev/null; then
  PYTHON_BIN="python3"
elif command -v python &> /dev/null; then
  PYTHON_BIN="python"
else
  echo "❌ python3 not found! Run ./install.sh (or install Python 3.10+)"
  exit 1
fi
echo "🐍 Using interpreter: $PYTHON_BIN"

# Check config.json exists
if [ ! -f "config.json" ]; then
  echo "⚠️  config.json not found!"
  echo "Run: cp config.example.json config.json  (or ./install.sh)"
  exit 1
fi

# The dashboard is password protected and fails closed without a password
if [ -z "${DASHBOARD_PASSWORD:-}" ]; then
  echo "⚠️  DASHBOARD_PASSWORD is not set — nobody will be able to log in to the dashboard."
  echo "   Add DASHBOARD_USER / DASHBOARD_PASSWORD to .env (or re-run ./install.sh) and restart."
fi

# Ensure dependencies are importable
echo "🔍 Checking dependencies..."
"$PYTHON_BIN" -c "import telegram, yt_dlp, flask, requests" 2>/dev/null || {
  echo "⚠️  Some dependencies missing, installing..."
  "$PYTHON_BIN" -m pip install -r requirements.txt \
    || "$PYTHON_BIN" -m pip install --user -r requirements.txt
}

# Start bot and dashboard in background
echo "▶ Starting Telegram bot..."
"$PYTHON_BIN" bot.py &
BOT_PID=$!

# Give the bot a moment, then report honestly whether it is still alive
sleep 2
if kill -0 "$BOT_PID" 2>/dev/null; then
  BOT_STATUS="running (PID $BOT_PID)"
else
  BOT_STATUS="exited — check the output above (bad token? network?)"
fi

echo "▶ Starting web dashboard on http://${DASHBOARD_HOST:-0.0.0.0}:${DASHBOARD_PORT:-5000} ..."
"$PYTHON_BIN" dashboard/app.py &
DASH_PID=$!

# Wait (briefly) for the dashboard to accept connections
DASH_URL="http://127.0.0.1:${DASHBOARD_PORT:-5000}/healthz"
for _ in $(seq 1 15); do
  if command -v curl >/dev/null 2>&1; then
    curl -fsS -m 2 "$DASH_URL" >/dev/null 2>&1 && break
  else
    sleep 1 && break
  fi
  sleep 1
done

echo ""
echo "✅ AutoRepost Pipeline is up!"
echo "📱 Bot: $BOT_STATUS"
echo "🌐 Dashboard: http://localhost:${DASHBOARD_PORT:-5000} (PID $DASH_PID)"
echo "🔒 Dashboard login: ${DASHBOARD_USER:-admin} (password from .env)"
echo ""
echo "📋 Logs: tail -f logs/pipeline.log"
echo "🛑 Press Ctrl+C to stop both services"
echo ""

# Keep running, stop both on Ctrl+C
trap "echo ''; echo '🛑 Stopping services...'; kill $BOT_PID $DASH_PID 2>/dev/null || true; echo 'Stopped.'; exit 0" SIGINT SIGTERM

# Wait for both processes
wait
