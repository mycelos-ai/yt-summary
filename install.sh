#!/bin/sh
# yt-summary one-command installer.
#
# Usage (short, recommended):
#   curl -fsSL mycelos.com/yt-summary/install.sh | sh
#
# Usage (direct, if you don't trust the redirect):
#   curl -fsSL https://raw.githubusercontent.com/mycelos-ai/yt-summary/main/install.sh | sh
#
# Re-running this script on an existing install updates everything:
# refreshes docker-compose.yml, pulls the latest image from GHCR,
# and recreates the container only if the image actually changed.
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
HEALTH_URL="http://localhost:8200/api/v1/health"
IMAGE_REF="ghcr.io/mycelos-ai/yt-summary:latest"

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

# --- 3. Detect fresh install vs update --------------------------------------
STEP="detecting install mode"
if [ -f "$INSTALL_DIR/docker-compose.yml" ]; then
    MODE="update"
    say "Existing install detected at $INSTALL_DIR — updating in place."
else
    MODE="fresh"
    say "Fresh install at $INSTALL_DIR."
fi

# --- 4. Install dir + download compose file ---------------------------------
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

# --- 5. Pull (record whether the image actually changed) --------------------
# Note the image ID before/after so we can tell the user what
# happened. `docker image inspect` returns nothing when the image
# isn't present yet, which is fine on a fresh install.
STEP="checking current image"
BEFORE_ID=$(docker image inspect --format '{{.Id}}' "$IMAGE_REF" 2>/dev/null || true)

STEP="pulling latest image"
$COMPOSE pull

AFTER_ID=$(docker image inspect --format '{{.Id}}' "$IMAGE_REF" 2>/dev/null || true)
if [ -z "$BEFORE_ID" ]; then
    say "Pulled image: $AFTER_ID"
elif [ "$BEFORE_ID" = "$AFTER_ID" ]; then
    say "Image already up to date — no new release on GHCR."
else
    say "Pulled new image: $AFTER_ID"
    say "  (was: $BEFORE_ID)"
fi

# --- 6. Start (recreates the container only if the image changed) -----------
STEP="starting container"
$COMPOSE up -d

# --- 7. Healthcheck ---------------------------------------------------------
# Container is up, but uvicorn + sqlite migrations take a couple of
# seconds before /api/v1/health responds. Poll a few times before
# giving up — easier to reason about a clear "yes / no" than to
# trust the user to figure out container logs themselves.
STEP="waiting for app to become ready"
say "Waiting for the app to come up …"
HEALTHY=""
for i in 1 2 3 4 5 6 7 8 9 10; do
    if command -v curl >/dev/null 2>&1; then
        if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
            HEALTHY="yes"
            break
        fi
    elif command -v wget >/dev/null 2>&1; then
        if wget -qO- "$HEALTH_URL" >/dev/null 2>&1; then
            HEALTHY="yes"
            break
        fi
    fi
    # ~2s polling interval, total ~20s
    sleep 2
    printf '.'
done
printf '\n'

if [ -z "$HEALTHY" ]; then
    warn "App did not respond at $HEALTH_URL within 20 seconds."
    warn "Check container status: cd $INSTALL_DIR && $COMPOSE ps"
    warn "Check logs:             cd $INSTALL_DIR && $COMPOSE logs --tail=50"
    # Don't trap — the container is probably running, just slow.
    # Give the user the diagnostics they need and exit non-zero so
    # CI users see the failure.
    trap - EXIT INT TERM
    exit 2
fi

# --- 8. Success -------------------------------------------------------------
STEP="done"
trap - EXIT INT TERM

printf '\n'
if [ "$MODE" = "update" ]; then
    say "Update complete."
else
    say "yt-summary is running at http://localhost:8200"
fi
say "Open:        http://localhost:8200"
say "Settings:    http://localhost:8200/settings"
say "Data lives:  $INSTALL_DIR/data/"
say "To stop:     cd $INSTALL_DIR && $COMPOSE down"
say "To update:   re-run this same install command"
printf '\n'
