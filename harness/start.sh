#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
PORT=8888

# Load server/.env so webhook_server.py inherits all secrets
set -a
# shellcheck source=/dev/null
source "$ROOT/server/.env"
set +a

DOMAIN="${NGROK_DOMAIN:?NGROK_DOMAIN not set in server/.env}"

# Start ngrok with static domain in background
ngrok http --domain="$DOMAIN" "$PORT" --log=stdout > /tmp/ngrok-bayview.log 2>&1 &
NGROK_PID=$!
sleep 3  # give ngrok time to establish tunnel

echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "  🔗 Webhook URL → https://$DOMAIN/webhook/cekura"
echo "  📋 Configure in: Cekura → Agent Settings → Webhook URL"
echo "  🔑 Secret already set in server/.env as CEKURA_WEBHOOK_SECRET"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""

# Trap to clean up ngrok on exit
trap 'kill $NGROK_PID 2>/dev/null; echo "Stopped."' EXIT INT TERM

# Start webhook server (foreground) — cd into server/ so uv finds pyproject.toml
cd "$ROOT/server"
uv run python3 ../harness/webhook_server.py --port "$PORT"
