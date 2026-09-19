#!/usr/bin/env bash
# ==============================================================================
# AutoRepost Pipeline — One-Command Installer
#
# Usage (from a checkout):
#   ./install.sh
#
# Usage (one-liner, clones the repo for you):
#   curl -fsSL https://raw.githubusercontent.com/Nadim3x/reboter/main/install.sh | bash
#
# What it does:
#   1. Installs system dependencies (ffmpeg, python3) via the native package manager
#   2. Creates a Python virtual environment and installs requirements.txt
#   3. Creates downloads/ thumbnails/ logs/ and seeds config.json from the example
#   4. Optionally saves your Telegram token + Zernio API key
#   5. Optionally installs a systemd service so the pipeline runs 24/7
#
# Idempotent: safe to re-run — it upgrades dependencies instead of re-installing.
# ==============================================================================

set -euo pipefail

SCRIPT_VERSION="1.0.0"
DEFAULT_REPO_URL="${AUTOREPOST_REPO_URL:-https://github.com/Nadim3x/reboter.git}"
DEFAULT_INSTALL_DIR="${AUTOREPOST_DIR:-$HOME/AutoRepost-Pipeline}"

# --- Options -----------------------------------------------------------------
INSTALL_DIR=""
REPO_URL="$DEFAULT_REPO_URL"
ENV_TELEGRAM_TOKEN="${TELEGRAM_TOKEN:-}"
ENV_ZERNIO_API_KEY="${ZERNIO_API_KEY:-}"
TELEGRAM_TOKEN=""
ZERNIO_API_KEY=""
NON_INTERACTIVE=0
SKIP_SYSTEM_DEPS=0
SKIP_VENV=0
SKIP_UPDATE=0
INSTALL_SERVICE=0
START_AFTER=0
DRY_RUN=0

# --- Colors ------------------------------------------------------------------
if [ -t 1 ]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_BLUE=$'\033[34m'
else
  C_RESET=""; C_BOLD=""; C_DIM=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""
fi

log()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s%s%s\n' "$C_BLUE$C_BOLD" "$C_RESET" "$C_BOLD" "$*" "$C_RESET"; }
ok()   { printf '  %s✔%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '  %s!%s %s\n' "$C_YELLOW" "$C_RESET" "$*"; }
err()  { printf '  %s✘%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; }
die()  { err "$*"; exit 1; }

usage() {
  cat <<'EOF'
AutoRepost Pipeline — One-Command Installer

Usage:
  ./install.sh [options]
  curl -fsSL <raw-install-url> | bash -s -- [options]

Options:
  -d, --dir DIR              Install/clone directory (default: script directory,
                             or ~/AutoRepost-Pipeline when piped through curl)
  -t, --telegram-token TOK   Telegram bot token (skips the interactive prompt)
  -z, --zernio-key KEY       Zernio API key (skips the interactive prompt)
  -n, --non-interactive      Never prompt; use flags/env only
      --service              Install + enable a systemd service (starts on boot)
      --start                Start the pipeline (./start.sh) when finished
      --skip-system-deps     Do not install ffmpeg/python3 via the OS package manager
      --skip-venv            Do not create .venv; install with pip --user instead
      --skip-update          Do not git pull an existing checkout
      --repo-url URL         Git remote to clone when run outside a checkout
      --dry-run              Print what would happen, change nothing
  -v, --version              Print installer version
  -h, --help                 Show this help

Environment variables:
  AUTOREPOST_REPO_URL        Override the git remote used when cloning
  AUTOREPOST_DIR             Override the install directory
  TELEGRAM_TOKEN             Same as --telegram-token
  ZERNIO_API_KEY             Same as --zernio-key
  DASHBOARD_PORT             Dashboard port (default 5000, stored in .env)
EOF
}

# --- Arg parsing -------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    -d|--dir)              INSTALL_DIR="${2:-}"; shift 2 ;;
    -t|--telegram-token)   TELEGRAM_TOKEN="${2:-}"; shift 2 ;;
    -z|--zernio-key)       ZERNIO_API_KEY="${2:-}"; shift 2 ;;
    -n|--non-interactive)  NON_INTERACTIVE=1; shift ;;
    --service)             INSTALL_SERVICE=1; shift ;;
    --start)               START_AFTER=1; shift ;;
    --skip-system-deps)    SKIP_SYSTEM_DEPS=1; shift ;;
    --skip-venv)           SKIP_VENV=1; shift ;;
    --skip-update)         SKIP_UPDATE=1; shift ;;
    --repo-url)            REPO_URL="${2:-}"; shift 2 ;;
    --dry-run)             DRY_RUN=1; shift ;;
    -v|--version)          printf 'autorepost installer %s\n' "$SCRIPT_VERSION"; exit 0 ;;
    -h|--help)             usage; exit 0 ;;
    *) die "Unknown option: $1 (try --help)" ;;
  esac
done

# Flags win over env-provided credentials
TELEGRAM_TOKEN="${TELEGRAM_TOKEN:-$ENV_TELEGRAM_TOKEN}"
ZERNIO_API_KEY="${ZERNIO_API_KEY:-$ENV_ZERNIO_API_KEY}"

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '   %s[dry-run]%s %s\n' "$C_DIM" "$C_RESET" "$*"
    return 0
  fi
  "$@"
}

run_sh() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '   %s[dry-run]%s sh -c %s\n' "$C_DIM" "$C_RESET" "$1"
    return 0
  fi
  sh -c "$1"
}

have() { command -v "$1" >/dev/null 2>&1; }

# --- sudo handling -----------------------------------------------------------
SUDO=""
init_sudo() {
  if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
  elif have sudo; then
    SUDO="sudo"
  else
    SUDO=""
  fi
}

can_elevate() {
  [ "$(id -u)" -eq 0 ] || [ -n "$SUDO" ]
}

# ==============================================================================
# 1. Locate the source tree (checkout, or clone when piped through curl)
# ==============================================================================

resolve_script_dir() {
  local src="${BASH_SOURCE[0]:-}"
  if [ -n "$src" ] && [ -f "$src" ]; then
    (cd "$(dirname "$src")" && pwd)
  fi
}

SCRIPT_DIR="$(resolve_script_dir || true)"

is_checkout() {
  [ -n "${1:-}" ] && [ -f "$1/bot.py" ] && [ -f "$1/requirements.txt" ] && [ -f "$1/dashboard/app.py" ]
}

prepare_source() {
  step "Locating AutoRepost source"

  if [ -z "$INSTALL_DIR" ] && is_checkout "${SCRIPT_DIR:-}"; then
    INSTALL_DIR="$SCRIPT_DIR"
  fi
  INSTALL_DIR="${INSTALL_DIR:-$DEFAULT_INSTALL_DIR}"

  if is_checkout "$INSTALL_DIR"; then
    ok "Using existing checkout: $INSTALL_DIR"
    if [ "$SKIP_UPDATE" -eq 0 ] && [ -d "$INSTALL_DIR/.git" ] && have git; then
      log "   Updating checkout (git pull --ff-only)..."
      if run git -C "$INSTALL_DIR" pull --ff-only >/dev/null 2>&1; then
        ok "Checkout up to date"
      else
        warn "Could not fast-forward (local changes?) — continuing with current files"
      fi
    fi
    return 0
  fi

  # Not a checkout: clone it.
  have git || die "git is required to download the project. Install git and re-run."
  step "Cloning $REPO_URL"
  mkdir -p "$(dirname "$INSTALL_DIR")"
  if [ -d "$INSTALL_DIR/.git" ]; then
    ok "Existing git checkout found at $INSTALL_DIR"
  elif [ -e "$INSTALL_DIR" ]; then
    die "$INSTALL_DIR exists but is not an AutoRepost checkout. Use --dir to pick another path."
  else
    run git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
    ok "Cloned into $INSTALL_DIR"
  fi
  is_checkout "$INSTALL_DIR" || die "Clone does not look like the AutoRepost repo (missing bot.py)."
}

# ==============================================================================
# 2. System dependencies
# ==============================================================================

detect_os() {
  if [ "$(uname -s)" = "Darwin" ]; then
    echo "macos"
  elif [ -f /etc/alpine-release ]; then
    echo "alpine"
  elif [ -f /etc/debian_version ]; then
    echo "debian"
  elif [ -f /etc/fedora-release ]; then
    echo "fedora"
  elif [ -f /etc/arch-release ]; then
    echo "arch"
  elif [ -f /etc/os-release ] && grep -qiE 'suse|opensuse' /etc/os-release; then
    echo "suse"
  elif [ -f /etc/os-release ] && grep -qiE 'rhel|centos|rocky|almalinux' /etc/os-release; then
    echo "rhel"
  else
    echo "unknown"
  fi
}

install_system_deps() {
  if [ "$SKIP_SYSTEM_DEPS" -eq 1 ]; then
    warn "Skipping system packages (--skip-system-deps)"
    return 0
  fi

  OS="$(detect_os)"
  step "Installing system dependencies (ffmpeg, python3) — detected: $OS"

  local cmd=""
  case "$OS" in
    alpine) cmd="$SUDO apk add --no-cache python3 py3-pip py3-virtualenv ffmpeg" ;;
    debian) cmd="$SUDO apt-get update && $SUDO apt-get install -y python3 python3-venv python3-pip ffmpeg" ;;
    fedora) cmd="$SUDO dnf install -y python3 python3-pip ffmpeg" ;;
    rhel)   cmd="$SUDO dnf install -y python3 python3-pip ffmpeg || $SUDO yum install -y python3 python3-pip ffmpeg" ;;
    arch)   cmd="$SUDO pacman -Sy --noconfirm python python-pip ffmpeg" ;;
    suse)   cmd="$SUDO zypper --non-interactive install python3 python3-pip ffmpeg" ;;
    macos)  cmd="brew install python ffmpeg" ;;
    *)      warn "Unsupported OS — install ffmpeg + Python 3.10+ manually, then re-run with --skip-system-deps"; return 0 ;;
  esac

  if ! can_elevate && [ "$(id -u)" -ne 0 ]; then
    warn "No root/sudo available — skipping OS packages (install ffmpeg manually if missing)"
    return 0
  fi

  if run_sh "$cmd"; then
    ok "System packages installed"
  else
    warn "Package installation reported an error (ffmpeg may come from a different repo) — continuing"
  fi
}

detect_python() {
  local candidate
  for candidate in python3 python; do
    if have "$candidate"; then
      if "$candidate" - <<'PY' >/dev/null 2>&1
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY
      then
        command -v "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

check_requirements() {
  step "Checking prerequisites"

  if ! PYTHON_BIN_BASE="$(detect_python)"; then
    err "Python 3.10+ not found."
    log "   Alpine:  apk add python3 py3-pip"
    log "   Ubuntu:  sudo apt install python3 python3-venv"
    die "Install Python 3.10+ and re-run this installer."
  fi
  ok "Python: $("$PYTHON_BIN_BASE" --version 2>&1) ($PYTHON_BIN_BASE)"

  if have ffmpeg; then
    ok "ffmpeg: $(ffmpeg -version 2>/dev/null | head -n1 | awk '{print $3}')"
  else
    warn "ffmpeg not found — yt-dlp needs it to merge video/audio streams"
  fi
}

# ==============================================================================
# 3. Python environment + dependencies
# ==============================================================================

install_with_pip_user() {
  local pip_bin
  pip_bin="$(command -v pip3 || command -v pip || true)"
  [ -n "$pip_bin" ] || die "pip is not available. Install python3-pip and re-run."

  log "   Installing requirements with $pip_bin --user ..."
  if ! run "$pip_bin" install --user -r "$INSTALL_DIR/requirements.txt"; then
    warn "Retrying with --break-system-packages (PEP 668 managed environment)"
    run "$pip_bin" install --user --break-system-packages -r "$INSTALL_DIR/requirements.txt" \
      || die "Failed to install Python dependencies."
  fi
  PYTHON_BIN="$PYTHON_BIN_BASE"
}

setup_python_env() {
  local venv_dir="$INSTALL_DIR/.venv"

  if [ "$SKIP_VENV" -eq 1 ]; then
    step "Installing Python dependencies (no virtualenv)"
    install_with_pip_user
    return 0
  fi

  step "Setting up Python virtual environment"
  if [ -x "$venv_dir/bin/python" ]; then
    ok "Reusing existing virtualenv: $venv_dir"
  else
    if run "$PYTHON_BIN_BASE" -m venv "$venv_dir"; then
      ok "Created virtualenv: $venv_dir"
    else
      warn "Could not create a virtualenv (missing python3-venv?) — falling back to pip --user"
      install_with_pip_user
      return 0
    fi
  fi

  PYTHON_BIN="$venv_dir/bin/python"
  log "   Upgrading pip..."
  run "$PYTHON_BIN" -m pip install --upgrade pip setuptools wheel >/dev/null
  log "   Installing requirements.txt (this can take a minute)..."
  run "$PYTHON_BIN" -m pip install -r "$INSTALL_DIR/requirements.txt" \
    || die "Dependency installation failed. Scroll up for the pip error."
  ok "Python dependencies installed"
}

# ==============================================================================
# 4. Directories + config
# ==============================================================================

setup_directories() {
  step "Creating project directories"
  run mkdir -p "$INSTALL_DIR/downloads" "$INSTALL_DIR/thumbnails" "$INSTALL_DIR/logs"
  ok "downloads/  thumbnails/  logs/"
}

set_config_value() {
  local key="$1" value="$2" config_path="$INSTALL_DIR/config.json"
  [ "$DRY_RUN" -eq 1 ] && return 0
  KEY="$key" VALUE="$value" CONFIG_PATH="$config_path" "$PYTHON_BIN" - <<'PY'
import json, os
path = os.environ["CONFIG_PATH"]
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)
data[os.environ["KEY"]] = os.environ["VALUE"]
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
PY
}

read_config_value() {
  local key="$1" config_path="$INSTALL_DIR/config.json"
  [ -f "$config_path" ] || return 0
  KEY="$key" CONFIG_PATH="$config_path" "$PYTHON_BIN" - <<'PY' 2>/dev/null || true
import json, os
with open(os.environ["CONFIG_PATH"], "r", encoding="utf-8") as fh:
    value = json.load(fh).get(os.environ["KEY"], "")
print(value if isinstance(value, str) else "")
PY
}

prompt_tty() {
  # Reads a line from the controlling terminal (safe under `curl | bash`).
  local __var="$1" __prompt="$2" __value=""
  if [ -r /dev/tty ]; then
    printf '%s' "$__prompt" > /dev/tty
    IFS= read -r __value < /dev/tty || true
    printf '\n' > /dev/tty
  fi
  printf -v "$__var" '%s' "$__value"
}

is_placeholder() {
  case "${1:-}" in
    ""|YOUR_TELEGRAM_BOT_TOKEN|YOUR_ZERNIO_API_KEY) return 0 ;;
    *) return 1 ;;
  esac
}

setup_config() {
  step "Configuring config.json"
  local config_path="$INSTALL_DIR/config.json"

  if [ ! -f "$config_path" ]; then
    run cp "$INSTALL_DIR/config.example.json" "$config_path"
    ok "Created config.json from config.example.json"
  else
    ok "config.json already exists — keeping your settings"
  fi

  # Fill in tokens provided via flag/env
  if ! is_placeholder "$TELEGRAM_TOKEN"; then
    set_config_value "telegram_token" "$TELEGRAM_TOKEN"
    ok "Telegram token saved"
  fi
  if ! is_placeholder "$ZERNIO_API_KEY"; then
    set_config_value "zernio_api_key" "$ZERNIO_API_KEY"
    ok "Zernio API key saved"
  fi

  # Interactive prompts (skipped with --non-interactive or when already set)
  if [ "$NON_INTERACTIVE" -eq 0 ] && [ -r /dev/tty ]; then
    if is_placeholder "$TELEGRAM_TOKEN" && is_placeholder "$(read_config_value telegram_token)"; then
      log "   Create a bot with @BotFather on Telegram to get a token."
      log "   (Leave blank to fill in later via the dashboard at /settings)"
      prompt_tty TELEGRAM_TOKEN "   Telegram bot token: "
      if ! is_placeholder "$TELEGRAM_TOKEN"; then
        set_config_value "telegram_token" "$TELEGRAM_TOKEN"
        ok "Telegram token saved"
      else
        warn "No Telegram token yet — set it later in the dashboard"
      fi
    fi

    if is_placeholder "$ZERNIO_API_KEY" && is_placeholder "$(read_config_value zernio_api_key)"; then
      prompt_tty ZERNIO_API_KEY "   Zernio API key: "
      if ! is_placeholder "$ZERNIO_API_KEY"; then
        set_config_value "zernio_api_key" "$ZERNIO_API_KEY"
        ok "Zernio API key saved"
      else
        warn "No Zernio key yet — set it later in the dashboard"
      fi
    fi

    if [ -z "${DASHBOARD_PORT:-}" ]; then
      local chosen_port
      prompt_tty chosen_port "   Dashboard port [5000]: "
      DASHBOARD_PORT="${chosen_port:-5000}"
    fi
  fi

  # Persist dashboard host/port so start.sh and the Flask app agree
  if [[ "${DASHBOARD_PORT:-5000}" =~ ^[0-9]+$ ]] && [ "${DASHBOARD_PORT:-5000}" != "5000" ]; then
    write_env_file "$DASHBOARD_PORT"
  fi
}

write_env_file() {
  local port="$1"
  [ "$DRY_RUN" -eq 1 ] && return 0
  {
    echo "# AutoRepost Pipeline environment — created by install.sh"
    echo "DASHBOARD_HOST=0.0.0.0"
    echo "DASHBOARD_PORT=$port"
  } > "$INSTALL_DIR/.env"
  ok "Wrote .env (dashboard port $port)"
}

# ==============================================================================
# 5. systemd service (optional)
# ==============================================================================

install_service() {
  [ "$INSTALL_SERVICE" -eq 1 ] || return 0
  step "Installing systemd service"

  if [ "$(uname -s)" != "Linux" ] || ! have systemctl; then
    warn "systemd not available — skipping service. Start manually with ./start.sh"
    return 0
  fi

  local unit="[Unit]
Description=AutoRepost Pipeline (Telegram bot + dashboard)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/env bash $INSTALL_DIR/start.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"

  if can_elevate; then
    if [ "$DRY_RUN" -eq 0 ]; then
      printf '%s' "$unit" | $SUDO tee /etc/systemd/system/autorepost.service >/dev/null
      $SUDO systemctl daemon-reload
      $SUDO systemctl enable --now autorepost.service
    fi
    ok "Service installed: systemctl status autorepost"
  elif have systemctl && systemctl --user show-environment >/dev/null 2>&1; then
    mkdir -p "$HOME/.config/systemd/user"
    if [ "$DRY_RUN" -eq 0 ]; then
      printf '%s' "$unit" | sed 's/WantedBy=multi-user.target/WantedBy=default.target/' \
        > "$HOME/.config/systemd/user/autorepost.service"
      systemctl --user daemon-reload
      systemctl --user enable --now autorepost.service
    fi
    ok "User service installed: systemctl --user status autorepost"
    warn "Run 'loginctl enable-linger $USER' to keep it running when you log out"
  else
    warn "No root or user systemd session — start manually with ./start.sh"
  fi
}

# ==============================================================================
# 6. Verify
# ==============================================================================

verify_install() {
  step "Verifying installation"
  [ "$DRY_RUN" -eq 1 ] && { warn "Dry run — skipping verification"; return 0; }

  if ! "$PYTHON_BIN" -c "import telegram, yt_dlp, flask, requests" 2>/dev/null; then
    err "Some Python packages failed to import."
    die "Re-run: $PYTHON_BIN -m pip install -r $INSTALL_DIR/requirements.txt"
  fi
  ok "Python imports OK (telegram, yt-dlp, flask, requests)"

  local py_ok=1
  for f in bot.py downloader.py caption.py uploader.py dashboard/app.py; do
    "$PYTHON_BIN" -m py_compile "$INSTALL_DIR/$f" 2>/dev/null || { py_ok=0; err "Syntax check failed: $f"; }
  done
  [ "$py_ok" -eq 1 ] && ok "All modules compile"
}

# ==============================================================================
# Main
# ==============================================================================

main() {
  printf '\n%s%s AutoRepost Pipeline — Installer v%s %s\n' "$C_BOLD" "$C_BLUE" "$SCRIPT_VERSION" "$C_RESET"
  [ "$DRY_RUN" -eq 1 ] && warn "Dry run mode — nothing will be changed"

  init_sudo
  prepare_source
  install_system_deps
  check_requirements
  setup_python_env
  setup_directories
  setup_config
  install_service
  verify_install

  local port="${DASHBOARD_PORT:-}"
  if [ -f "$INSTALL_DIR/.env" ]; then
    local env_port
    env_port="$(grep -E '^DASHBOARD_PORT=' "$INSTALL_DIR/.env" 2>/dev/null | cut -d= -f2 || true)"
    port="${env_port:-$port}"
  fi
  port="${port:-5000}"
  local token_ok="missing"
  is_placeholder "$(read_config_value telegram_token)" || token_ok="set"

  printf '\n%s%s Installation complete %s\n\n' "$C_BOLD" "$C_GREEN" "$C_RESET"
  log "  Project:    $INSTALL_DIR"
  log "  Python:     $PYTHON_BIN"
  log "  Dashboard:  http://localhost:$port   (Telegram token: $token_ok)"
  log "  Logs:       $INSTALL_DIR/logs/pipeline.log"
  printf '\n'
  log "  Next steps:"
  log "    1. cd $INSTALL_DIR"
  if [ "$token_ok" = "missing" ] || is_placeholder "$(read_config_value zernio_api_key)"; then
    log "    2. Add your tokens:  ./start.sh  then open the dashboard at /settings"
    log "       (or edit $INSTALL_DIR/config.json directly)"
  else
    log "    2. Tokens look good — nothing else to configure"
  fi
  log "    3. Start it:  ./start.sh"
  log "       Background:  systemctl status autorepost   (if installed with --service)"
  printf '\n'

  if [ "$START_AFTER" -eq 1 ] && [ "$DRY_RUN" -eq 0 ]; then
    step "Starting AutoRepost Pipeline"
    cd "$INSTALL_DIR" && exec ./start.sh
  fi
}

main "$@"
