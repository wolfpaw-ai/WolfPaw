#!/usr/bin/env bash
# Wolfpaw — first-time self-host setup.
#
# Idempotent: safe to re-run. Doesn't touch an existing .env. Doesn't
# rebuild containers that are already up.
#
# What it does:
#   1. Verifies docker + docker compose are installed.
#   2. Copies .env.example → .env on first run.
#   3. Generates a random WOLFPAW_SECRET_KEY if the .env still has the
#      placeholder.
#   4. Reminds you to fill in the required keys.
#   5. Builds + starts the stack (skip with NO_UP=1 if you'd rather do
#      it yourself).

set -euo pipefail

cd "$(dirname "$0")"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
warn() { printf "\033[33m%s\033[0m\n" "$*"; }
fail() { printf "\033[31m%s\033[0m\n" "$*" >&2; exit 1; }

bold "Wolfpaw self-host setup"

# --- 1. prerequisites ----------------------------------------------------

command -v docker >/dev/null 2>&1 || fail "docker is not installed (see https://docs.docker.com/get-docker/)"
docker compose version >/dev/null 2>&1 || fail "docker compose plugin is not installed"

# --- 2. .env --------------------------------------------------------------

if [ ! -f .env ]; then
    cp .env.example .env
    echo "created .env from template"
else
    echo ".env already exists — leaving it alone"
fi

# --- 3. secret key --------------------------------------------------------

# Generate a real secret if the user hasn't set one (or left it blank).
if ! grep -qE '^WOLFPAW_SECRET_KEY=.+' .env || grep -qE '^WOLFPAW_SECRET_KEY=\s*$' .env; then
    if command -v openssl >/dev/null 2>&1; then
        SECRET=$(openssl rand -hex 32)
    else
        SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
    fi
    # Cross-platform sed in-place: write to a temp file, then move.
    awk -v secret="$SECRET" '
        BEGIN { done = 0 }
        /^WOLFPAW_SECRET_KEY=/ { print "WOLFPAW_SECRET_KEY=" secret; done = 1; next }
        { print }
        END { if (!done) print "WOLFPAW_SECRET_KEY=" secret }
    ' .env > .env.tmp && mv .env.tmp .env
    echo "generated WOLFPAW_SECRET_KEY"
fi

# --- 4. required keys -----------------------------------------------------

missing=()
if ! grep -qE '^WOLFPAW_ANTHROPIC_API_KEY=.+' .env || grep -qE '^WOLFPAW_ANTHROPIC_API_KEY=\s*$' .env; then
    missing+=("WOLFPAW_ANTHROPIC_API_KEY")
fi

if [ ${#missing[@]} -gt 0 ]; then
    warn ""
    warn "Before the app does anything useful, set these in .env:"
    for k in "${missing[@]}"; do warn "  - $k"; done
    warn ""
    warn "Then re-run this script, or:  docker compose up -d --build"
    exit 0
fi

# --- 5. up ----------------------------------------------------------------

if [ "${NO_UP:-0}" = "1" ]; then
    echo "NO_UP=1 — skipping 'docker compose up'. Run it yourself when ready."
    exit 0
fi

bold ""
bold "Starting Wolfpaw (this builds images on first run — a few minutes)..."
docker compose up -d --build

bold ""
bold "Up. The web UI is at:  http://localhost:${WOLFPAW_WEB_PORT:-3000}"
bold "Logs:                  docker compose logs -f app"
bold "Stop:                  docker compose down"
