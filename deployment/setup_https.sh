#!/bin/bash
# ==============================================
# HTTPS reverse proxy for the dashboard (Caddy)
#
# Uvicorn listens on 127.0.0.1:8501 only (deploy.sh). This script puts Caddy in
# front of it on 80/443 and gets a Let's Encrypt certificate automatically.
#
# Domain: $DASHBOARD_DOMAIN (env or .env). Without one, deploy.yml derives
# <public-ip-with-dashes>.sslip.io, which resolves to the server IP with no signup.
#
# Idempotent: safe to run on every deploy. Called by deploy.yml as non-fatal so a
# proxy problem never blocks a bot deploy.
#
# Not done here (needs the Oracle Cloud console): VCN Security List ingress for
# 80/tcp and 443/tcp. Without it Let's Encrypt cannot reach the server.
# ==============================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CADDYFILE=/etc/caddy/Caddyfile
MARKER="# managed by AlphaTrader deployment/setup_https.sh"
UPSTREAM="127.0.0.1:8501"

log() { echo "[https] $*"; }

if [ -z "${DASHBOARD_DOMAIN:-}" ] && [ -f "$PROJECT_DIR/.env" ]; then
    DASHBOARD_DOMAIN="$(grep -E '^DASHBOARD_DOMAIN=' "$PROJECT_DIR/.env" | tail -n1 | cut -d= -f2- || true)"
fi
DASHBOARD_DOMAIN="$(echo "${DASHBOARD_DOMAIN:-}" | tr -d '[:space:]')"
if [ -z "$DASHBOARD_DOMAIN" ]; then
    log "DASHBOARD_DOMAIN not set — skipping HTTPS setup"
    exit 0
fi
if ! echo "$DASHBOARD_DOMAIN" | grep -Eq '^[A-Za-z0-9.-]+$'; then
    log "invalid DASHBOARD_DOMAIN: $DASHBOARD_DOMAIN"
    exit 1
fi

if ! sudo -n true 2>/dev/null; then
    log "passwordless sudo unavailable — cannot install Caddy"
    exit 1
fi

# 1. Install Caddy (official apt repo)
if ! command -v caddy >/dev/null 2>&1; then
    if ! command -v apt-get >/dev/null 2>&1; then
        log "apt-get not found — install Caddy manually (https://caddyserver.com/docs/install)"
        exit 1
    fi
    log "installing Caddy..."
    APT="sudo -n DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=180 -y -q"
    $APT install debian-keyring debian-archive-keyring apt-transport-https curl gnupg >/dev/null
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | sudo -n gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        | sudo -n tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    $APT update >/dev/null
    $APT install caddy >/dev/null
    log "Caddy installed: $(caddy version | head -n1)"
fi

# 2. Caddyfile — only overwrite our own file or the package default
NEW_CONF="$(mktemp)"
trap 'rm -f "$NEW_CONF"' EXIT
cat > "$NEW_CONF" <<EOF
$MARKER
$DASHBOARD_DOMAIN {
	encode gzip
	reverse_proxy $UPSTREAM
}
EOF

if sudo -n test -f "$CADDYFILE" && ! sudo -n grep -qF "$MARKER" "$CADDYFILE"; then
    BACKUP="$CADDYFILE.bak.$(date +%Y%m%d%H%M%S)"
    sudo -n cp "$CADDYFILE" "$BACKUP"
    log "existing Caddyfile backed up to $BACKUP"
fi

if sudo -n test -f "$CADDYFILE" && sudo -n cmp -s "$NEW_CONF" "$CADDYFILE"; then
    log "Caddyfile unchanged ($DASHBOARD_DOMAIN -> $UPSTREAM)"
else
    caddy validate --adapter caddyfile --config "$NEW_CONF" >/dev/null 2>&1 || {
        log "generated Caddyfile failed validation"; exit 1; }
    sudo -n install -m 644 "$NEW_CONF" "$CADDYFILE"
    log "Caddyfile written ($DASHBOARD_DOMAIN -> $UPSTREAM)"
fi

# 3. Host firewall. Oracle's Ubuntu images end INPUT with a REJECT rule, so
#    opening the VCN Security List alone is not enough.
if command -v iptables >/dev/null 2>&1; then
    CHANGED=0
    for PORT in 80 443; do
        if ! sudo -n iptables -C INPUT -p tcp --dport "$PORT" -m state --state NEW -j ACCEPT 2>/dev/null; then
            sudo -n iptables -I INPUT 1 -p tcp --dport "$PORT" -m state --state NEW -j ACCEPT
            CHANGED=1
            log "iptables: opened $PORT/tcp"
        fi
    done
    if [ "$CHANGED" = 1 ] && command -v netfilter-persistent >/dev/null 2>&1; then
        sudo -n netfilter-persistent save >/dev/null 2>&1 && log "iptables rules persisted"
    fi
fi

# 4. (Re)start Caddy
sudo -n systemctl enable caddy >/dev/null 2>&1 || true
if sudo -n systemctl is-active --quiet caddy; then
    sudo -n systemctl reload caddy
else
    sudo -n systemctl restart caddy
fi

if sudo -n systemctl is-active --quiet caddy; then
    log "✅ Caddy running — https://$DASHBOARD_DOMAIN"
else
    log "❌ Caddy not running"
    sudo -n journalctl -u caddy -n 30 --no-pager || true
    exit 1
fi
