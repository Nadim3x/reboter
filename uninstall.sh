#!/usr/bin/env bash
# ==============================================================================
# AutoRepost Pipeline — Uninstaller
#
# Removes the systemd service and the virtualenv. Your config.json,
# thumbnails and logs are preserved unless you pass --purge.
#
# Usage:
#   ./uninstall.sh                # stop service, remove .venv (keep data)
#   ./uninstall.sh --purge        # also delete config.json, downloads, logs
# ==============================================================================

set -euo pipefail

PURGE=0
INSTALL_DIR=""
ASSUME_YES=0

while [ $# -gt 0 ]; do
  case "$1" in
    -d|--dir)   INSTALL_DIR="${2:-}"; shift 2 ;;
    -y|--yes)   ASSUME_YES=1; shift ;;
    --purge)    PURGE=1; shift ;;
    -h|--help)  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [ -z "$INSTALL_DIR" ]; then
  src="${BASH_SOURCE[0]:-}"
  if [ -n "$src" ] && [ -f "$src" ]; then
    INSTALL_DIR="$(cd "$(dirname "$src")" && pwd)"
  else
    INSTALL_DIR="$HOME/AutoRepost-Pipeline"
  fi
fi

SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
fi

echo "🗑️  Uninstalling AutoRepost Pipeline from $INSTALL_DIR"

# 1. Stop and remove services ------------------------------------------------
if command -v systemctl >/dev/null 2>&1; then
  if [ -f /etc/systemd/system/autorepost.service ]; then
    $SUDO systemctl disable --now autorepost.service 2>/dev/null || true
    $SUDO rm -f /etc/systemd/system/autorepost.service
    $SUDO systemctl daemon-reload 2>/dev/null || true
    echo "  ✔ Removed system service"
  fi
  if [ -f "$HOME/.config/systemd/user/autorepost.service" ]; then
    systemctl --user disable --now autorepost.service 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/autorepost.service"
    systemctl --user daemon-reload 2>/dev/null || true
    echo "  ✔ Removed user service"
  fi
fi

# 2. Stop any stray local processes -------------------------------------------
pkill -f "python3 .*bot.py" 2>/dev/null || true
pkill -f "dashboard/app.py" 2>/dev/null || true

# 3. Remove the virtualenv ----------------------------------------------------
if [ -d "$INSTALL_DIR/.venv" ]; then
  rm -rf "$INSTALL_DIR/.venv"
  echo "  ✔ Removed .venv"
fi

# 4. Optionally purge user data ----------------------------------------------
if [ "$PURGE" -eq 1 ]; then
  if [ "$ASSUME_YES" -eq 0 ]; then
    printf 'Delete config.json, downloads/, logs/ and thumbnails/? [y/N] '
    read -r answer < /dev/tty || answer="n"
    case "$answer" in
      [yY]*) ;;
      *) echo "  – Keeping your data"; PURGE=0 ;;
    esac
  fi
fi

if [ "$PURGE" -eq 1 ]; then
  rm -rf "$INSTALL_DIR/config.json" "$INSTALL_DIR/.env" \
         "$INSTALL_DIR/downloads" "$INSTALL_DIR/logs" "$INSTALL_DIR/thumbnails"
  find "$INSTALL_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  echo "  ✔ Removed config.json, .env, downloads/, logs/, thumbnails/"
fi

echo "✅ Done. Project files remain in $INSTALL_DIR (delete the folder to finish)."
