#!/bin/bash
set -e
echo "🚀 Starting AutoRepost Pipeline..."

# Create required directories
mkdir -p downloads thumbnails logs

# Ensure .gitkeep files exist for empty dirs (for git)
touch downloads/.gitkeep 2>/dev/null || true
touch thumbnails/.gitkeep 2>/dev/null || true
touch logs/.gitkeep 2>/dev/null || true

# Check config.json exists
if [ ! -f "config.json" ]; then
  echo "⚠️  config.json not found!"
  echo "Run: cp config.example.json config.json"
  echo "Then add your Telegram token and Zernio API key"
  exit 1
fi

# Check python3 exists
if ! command -v python3 &> /dev/null; then
  echo "❌ python3 not found! Install Python 3.11+"
  exit 1
fi

# Optional: check if requirements installed
echo "🔍 Checking dependencies..."
python3 -c "import telegram, yt_dlp, flask, requests" 2>/dev/null || {
  echo "⚠️  Some dependencies missing, installing..."
  pip install -r requirements.txt || pip3 install -r requirements.txt
}

# Start bot and dashboard in background
echo "▶ Starting Telegram bot..."
python3 bot.py &
BOT_PID=$!

# Small delay to let bot start
sleep 2

echo "▶ Starting web dashboard..."
python3 dashboard/app.py &
DASH_PID=$!

echo ""
echo "✅ AutoRepost Pipeline is running!"
echo "📱 Bot: Active (PID $BOT_PID)"
echo "🌐 Dashboard: http://localhost:5000 (PID $DASH_PID)"
echo ""
echo "📋 Logs: tail -f logs/pipeline.log"
echo "🛑 Press Ctrl+C to stop both services"
echo ""

# Keep running, stop both on Ctrl+C
trap "echo ''; echo '🛑 Stopping services...'; kill $BOT_PID $DASH_PID 2>/dev/null || true; echo 'Stopped.'; exit 0" SIGINT SIGTERM

# Wait for both processes
wait
