#!/usr/bin/env bash
###############################################################################
# bootstrap.sh — bring a fresh Ubuntu VPS to a state where the project's
# ansible deploys can run.
#
# Designed to be run AS ROOT on the target node (e.g. us-ny1). Idempotent:
# safe to re-run. Two-phase by design — the default pass leaves SSH wide open
# so you don't lock yourself out before verifying Tailscale SSH works; the
# `--harden` pass disables root SSH login and password auth.
#
# REQUIRED ENV VARS (default pass):
#   NODE_NAME          Cross-node-convention name (e.g. us-ny1, us-tx1, nl1).
#                      Sets both the system hostname AND the Tailscale name —
#                      otherwise fresh VPS images come up as "vultr"/"ubuntu".
#   DEPLOY_PUBKEY      Full ssh public key string for the deploy user
#                      (the public half of the GitHub DEPLOY_SSH_KEY secret).
#   TAILSCALE_AUTHKEY  One-shot or reusable Tailscale auth key from
#                      https://login.tailscale.com/admin/settings/keys
#                      (only required on first run; skipped if already up).
#
# OPTIONAL ENV VARS:
#   DEPLOY_USER        Default: frionode. Cross-node convention.
#   SWAP_GB            Default: 2. Skipped if any swap is already enabled.
#
# Usage:
#   # Phase 1: copy script up and run as root
#   scp bootstrap.sh root@us-ny1:/root/
#   ssh root@us-ny1
#   export NODE_NAME="us-ny1"
#   export DEPLOY_PUBKEY="ssh-ed25519 AAAA... aaron@deploy"
#   export TAILSCALE_AUTHKEY="tskey-auth-..."
#   bash /root/bootstrap.sh
#
#   # ...verify Tailscale SSH works:
#   #     tailscale ssh frionode@us-ny1   (from your laptop)
#   # ...then come back as root and harden:
#
#   bash /root/bootstrap.sh --harden
###############################################################################
set -euo pipefail

DEPLOY_USER="${DEPLOY_USER:-frionode}"
SWAP_GB="${SWAP_GB:-2}"
HARDEN_MODE=false
if [[ "${1:-}" == "--harden" ]]; then HARDEN_MODE=true; fi

# --- helpers ----------------------------------------------------------------
log()  { echo -e "\033[1;36m[bootstrap]\033[0m $*"; }
warn() { echo -e "\033[1;33m[bootstrap]\033[0m $*" >&2; }
err()  { echo -e "\033[1;31m[bootstrap]\033[0m $*" >&2; exit 1; }

require_root() {
    if [[ "$(id -u)" -ne 0 ]]; then
        err "must be run as root (got uid $(id -u))"
    fi
}

require_ubuntu() {
    if ! grep -qi ubuntu /etc/os-release 2>/dev/null; then
        err "this script targets Ubuntu — got $(. /etc/os-release && echo "$NAME $VERSION")"
    fi
}

# --- harden phase -----------------------------------------------------------
harden_phase() {
    log "Hardening phase: disable root SSH login + password auth"

    if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
        err "$DEPLOY_USER does not exist — run the default phase first"
    fi
    USER_HOME=$(getent passwd "$DEPLOY_USER" | cut -d: -f6)
    if [[ ! -s "$USER_HOME/.ssh/authorized_keys" ]]; then
        err "$USER_HOME/.ssh/authorized_keys is empty — refusing to lock you out"
    fi

    sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
    sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
    sed -i 's/^#\?PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config

    if systemctl is-active --quiet ssh; then
        systemctl reload ssh
    elif systemctl is-active --quiet sshd; then
        systemctl reload sshd
    fi
    log "sshd reloaded — root SSH disabled, password auth disabled"
    log "Done. From your laptop, you should now be able to:"
    log "    ssh $DEPLOY_USER@<node>      (over Tailscale or public)"
    log "    tailscale ssh $DEPLOY_USER@<node>"
}

# --- default phase ----------------------------------------------------------
default_phase() {
    require_ubuntu

    if [[ -z "${NODE_NAME:-}" ]]; then
        err "NODE_NAME env var is required (e.g. us-ny1, us-tx1) — sets system + Tailscale hostname"
    fi
    if [[ ! "$NODE_NAME" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]]; then
        err "NODE_NAME must be a valid hostname label (lowercase, digits, hyphens; got '$NODE_NAME')"
    fi
    if [[ -z "${DEPLOY_PUBKEY:-}" ]]; then
        err "DEPLOY_PUBKEY env var is required (the ssh-ed25519/rsa public key string)"
    fi

    export DEBIAN_FRONTEND=noninteractive

    log "1/9  apt update + base packages"
    apt-get update -qq
    apt-get install -y -qq \
        curl ca-certificates gnupg rsync ufw unattended-upgrades \
        apt-transport-https software-properties-common

    log "2/9  Set system hostname to '$NODE_NAME'"
    if [[ "$(hostname)" != "$NODE_NAME" ]]; then
        hostnamectl set-hostname "$NODE_NAME"
        # Keep /etc/hosts in sync so sudo doesn't complain about hostname lookup
        if grep -qE '^127\.0\.1\.1\s' /etc/hosts; then
            sed -i "s/^127\.0\.1\.1\s.*/127.0.1.1 $NODE_NAME/" /etc/hosts
        else
            echo "127.0.1.1 $NODE_NAME" >> /etc/hosts
        fi
        log "    hostname set; sudo may complain once until next login"
    else
        log "    already $NODE_NAME"
    fi

    log "3/9  Create deploy user '$DEPLOY_USER' with sudo NOPASSWD"
    if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
        useradd -m -s /bin/bash "$DEPLOY_USER"
        log "    created user $DEPLOY_USER"
    else
        log "    user $DEPLOY_USER already exists"
    fi
    usermod -aG sudo "$DEPLOY_USER"
    SUDOERS_FILE="/etc/sudoers.d/90-$DEPLOY_USER"
    if [[ ! -f "$SUDOERS_FILE" ]]; then
        echo "$DEPLOY_USER ALL=(ALL) NOPASSWD:ALL" > "$SUDOERS_FILE"
        chmod 440 "$SUDOERS_FILE"
    fi

    log "4/9  Install authorized_keys for $DEPLOY_USER"
    USER_HOME=$(getent passwd "$DEPLOY_USER" | cut -d: -f6)
    SSH_DIR="$USER_HOME/.ssh"
    AUTH_KEYS="$SSH_DIR/authorized_keys"
    install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 700 "$SSH_DIR"
    touch "$AUTH_KEYS"
    chmod 600 "$AUTH_KEYS"
    if ! grep -qF "$DEPLOY_PUBKEY" "$AUTH_KEYS" 2>/dev/null; then
        echo "$DEPLOY_PUBKEY" >> "$AUTH_KEYS"
        log "    added deploy pubkey"
    else
        log "    deploy pubkey already present"
    fi
    chown -R "$DEPLOY_USER:$DEPLOY_USER" "$SSH_DIR"

    log "5/9  Tailscale install + auth as '$NODE_NAME'"
    if ! command -v tailscale >/dev/null 2>&1; then
        curl -fsSL https://tailscale.com/install.sh | sh
    fi
    if ! tailscale status >/dev/null 2>&1 || \
       ! tailscale status --json 2>/dev/null | grep -q '"BackendState":"Running"'; then
        if [[ -z "${TAILSCALE_AUTHKEY:-}" ]]; then
            err "TAILSCALE_AUTHKEY required (Tailscale not yet authenticated)"
        fi
        tailscale up --authkey="$TAILSCALE_AUTHKEY" --ssh \
                     --hostname="$NODE_NAME" --accept-dns=false
        log "    Tailscale up as $NODE_NAME"
    else
        # Already up — make sure the Tailscale name matches NODE_NAME
        # (handles re-runs where NODE_NAME has been changed)
        CURRENT_TS_NAME=$(tailscale status --json 2>/dev/null | grep -oE '"Self":\s*\{[^}]*"HostName":\s*"[^"]*"' | grep -oE '"HostName":\s*"[^"]*"' | sed 's/.*"HostName":\s*"\([^"]*\)".*/\1/')
        if [[ -n "$CURRENT_TS_NAME" && "$CURRENT_TS_NAME" != "$NODE_NAME" ]]; then
            tailscale set --hostname="$NODE_NAME"
            log "    Tailscale renamed: $CURRENT_TS_NAME → $NODE_NAME"
        else
            log "    Tailscale already running as $NODE_NAME"
        fi
    fi

    log "6/9  Docker engine + compose plugin (official repo)"
    if ! command -v docker >/dev/null 2>&1; then
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
            -o /etc/apt/keyrings/docker.asc
        chmod a+r /etc/apt/keyrings/docker.asc
        UBUNTU_CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME} stable" \
            > /etc/apt/sources.list.d/docker.list
        apt-get update -qq
        apt-get install -y -qq \
            docker-ce docker-ce-cli containerd.io \
            docker-buildx-plugin docker-compose-plugin
        systemctl enable --now docker
    fi
    usermod -aG docker "$DEPLOY_USER"

    log "7/9  UFW firewall (allow ssh, 80, 443; deny everything else inbound)"
    ufw default deny incoming  >/dev/null
    ufw default allow outgoing >/dev/null
    ufw allow OpenSSH >/dev/null
    ufw allow 80/tcp  >/dev/null
    ufw allow 443/tcp >/dev/null
    ufw --force enable >/dev/null

    log "8/9  Unattended-upgrades for security patches"
    cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
    systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true

    log "9/9  Swap ($SWAP_GB GB) if none configured"
    if swapon --show 2>/dev/null | grep -q .; then
        log "    swap already enabled — skipping"
    else
        fallocate -l "${SWAP_GB}G" /swapfile
        chmod 600 /swapfile
        mkswap /swapfile >/dev/null
        swapon /swapfile
        if ! grep -q '^/swapfile' /etc/fstab; then
            echo '/swapfile none swap sw 0 0' >> /etc/fstab
        fi
        log "    ${SWAP_GB}G swap enabled and fstab updated"
    fi

    cat <<EOF

\033[1;32m[bootstrap]\033[0m Phase 1 complete on $(hostname).

  Deploy user:   $DEPLOY_USER
  Tailscale:     $(tailscale ip -4 2>/dev/null | head -1 || echo '(check `tailscale status`)')
  Docker:        $(docker --version 2>/dev/null || echo 'not running')
  UFW:           $(ufw status | head -1)

Next steps:
  1. From your laptop, verify Tailscale SSH works:
       tailscale ssh ${DEPLOY_USER}@$(hostname)
  2. Verify regular SSH with the deploy key works (it will until --harden):
       ssh ${DEPLOY_USER}@<public-ip>
  3. Then re-run THIS script with --harden to disable root + password SSH:
       bash /root/bootstrap.sh --harden

EOF
}

# --- main -------------------------------------------------------------------
require_root
if $HARDEN_MODE; then
    harden_phase
else
    default_phase
fi
