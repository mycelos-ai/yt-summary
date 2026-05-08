#!/bin/sh
# yt-summary one-command installer.
#
# Usage (short, recommended):
#   curl -fsSL mycelos.com/yt-summary/install.sh | sh
#
# Usage (direct, if you don't trust the redirect):
#   curl -fsSL https://raw.githubusercontent.com/mycelos-ai/yt-summary/main/install.sh | sh
#
# Env vars:
#   YTS_DIR  install directory (default: ~/yt-summary)

set -eu

# Internal URL pulls always hit GitHub raw directly — bypasses the
# vanity redirect so the script keeps working even if mycelos.com is
# briefly down, and keeps the source-of-truth obviously public.
REPO_RAW="https://raw.githubusercontent.com/mycelos-ai/yt-summary/main"
COMPOSE_URL="${REPO_RAW}/docker-compose.yml"
INSTALL_DIR="${YTS_DIR:-${HOME}/yt-summary}"

# Track which step we're on so the trap can give a useful message.
STEP="starting"
on_err() {
    printf '\n[install.sh] failed during: %s\n' "$STEP" >&2
    exit 1
}
trap on_err EXIT INT TERM

say() { printf '==> %s\n' "$1"; }
warn() { printf '[!] %s\n' "$1" >&2; }

# --- 1. Docker present? -----------------------------------------------------
STEP="checking docker"
if ! command -v docker >/dev/null 2>&1; then
    warn "Docker is not installed."
    warn "  macOS:  https://www.docker.com/products/docker-desktop/"
    warn "  Linux:  curl -fsSL https://get.docker.com | sh"
    exit 1
fi

STEP="checking docker daemon"
if ! docker info >/dev/null 2>&1; then
    warn "Docker is installed but the daemon isn't responding."
    warn "Start Docker Desktop (macOS) or 'sudo systemctl start docker' (Linux)."
    exit 1
fi

# --- 2. Compose v2 plugin or v1 standalone? ---------------------------------
STEP="checking docker compose"
if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    warn "Neither 'docker compose' (v2 plugin) nor 'docker-compose' (v1) is available."
    warn "Install Docker Compose: https://docs.docker.com/compose/install/"
    exit 1
fi
say "Using compose: $COMPOSE"

# --- 3. Install dir + download compose file ---------------------------------
STEP="creating $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

STEP="downloading docker-compose.yml"
if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$COMPOSE_URL" -o docker-compose.yml
elif command -v wget >/dev/null 2>&1; then
    wget -qO docker-compose.yml "$COMPOSE_URL"
else
    warn "Need either curl or wget to download docker-compose.yml."
    exit 1
fi

# --- 4. Pull + start --------------------------------------------------------
STEP="pulling image"
$COMPOSE pull

STEP="starting container"
$COMPOSE up -d

# --- 5. Success -------------------------------------------------------------
STEP="done"
trap - EXIT INT TERM

printf '\n'
say "yt-summary is running at http://localhost:8200"
say "Settings:    http://localhost:8200/settings"
say "Data lives:  $INSTALL_DIR/data/"
say "To stop:     cd $INSTALL_DIR && $COMPOSE down"
printf '\n'
