#!/usr/bin/env bash
# Deploy / redeploy the monitor to the VPS.
# Idempotent. Run from local repo root.

set -euo pipefail

REMOTE="${REMOTE:-ctrip@racknerd19}"
APP_DIR="${APP_DIR:-/opt/ctrip-price-monitor}"
SERVICE_USER="${SERVICE_USER:-ctrip}"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    cat <<EOF
Usage: REMOTE=user@host scripts/deploy.sh

What it does:
  1. rsync repo to remote \$APP_DIR (excluding .venv, data, __pycache__)
  2. create venv if missing + install requirements.txt
  3. install systemd units (web + 3 timers)
  4. reload caddy (if Caddyfile changed)
  5. start / enable services

Requires ssh access to the VPS, with sudo without password.
EOF
    exit 0
fi

LOCAL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> rsync to $REMOTE:$APP_DIR"
rsync -avz --delete \
    --exclude '.venv' \
    --exclude 'data/' \
    --exclude '__pycache__' \
    --exclude '.pytest_cache' \
    --exclude '*.pyc' \
    --exclude '.env' \
    "$LOCAL_ROOT/" "$REMOTE:$APP_DIR/"

echo "==> setup venv"
ssh "$REMOTE" bash -s <<EOF
set -e
cd "$APP_DIR"

if [[ ! -d .venv ]]; then
    python3.11 -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

# 创建 service user（如果不存在）
if ! id -u $SERVICE_USER >/dev/null 2>&1; then
    sudo useradd --system --shell /usr/sbin/nologin --home-dir "$APP_DIR" $SERVICE_USER
fi
sudo chown -R $SERVICE_USER:$SERVICE_USER "$APP_DIR/data" 2>/dev/null || true

# systemd units
sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload

# web (always restart)
sudo systemctl enable --now ctrip-web.service

# timers (enable but don't run-now, will fire per schedule)
sudo systemctl enable ctrip-scraper.timer    ctrip-parser.timer    ctrip-retention.timer
sudo systemctl restart ctrip-scraper.timer   ctrip-parser.timer   ctrip-retention.timer || true

# caddy
if [[ -f /etc/caddy/Caddyfile ]] && ! sudo diff -q Caddyfile /etc/caddy/Caddyfile >/dev/null; then
    sudo cp Caddyfile /etc/caddy/Caddyfile
    sudo systemctl reload caddy || true
fi

echo "OK"
EOF

echo "==> done"