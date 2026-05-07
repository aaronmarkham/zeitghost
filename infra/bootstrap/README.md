# Node bootstrap

`bootstrap.sh` brings a fresh Ubuntu VPS up to the state where the project's
Ansible deploy playbook can take over. Designed to run as root on the target
node, idempotent, two-phase.

## What it does

1. `apt update` + base packages (`curl`, `rsync`, `ufw`, `unattended-upgrades`, ...)
2. Sets the system hostname to `NODE_NAME` (and updates `/etc/hosts`) — fresh
   Vultr/DO/etc. images come up with generic names like `vultr` or `ubuntu`
3. Creates the `frionode` deploy user (cross-project convention) with `sudo NOPASSWD`
4. Installs the deploy public key into `~frionode/.ssh/authorized_keys`
5. Installs Tailscale and authenticates with `--ssh` enabled, registering as
   `NODE_NAME` (re-runs also rename the existing Tailscale machine if you
   change `NODE_NAME`)
6. Installs Docker Engine + `compose` plugin from the official Docker apt repo
7. Configures UFW: deny incoming, allow `OpenSSH`/`80`/`443`
8. Enables `unattended-upgrades` for security patches
9. Adds 2 GB swap if none is configured

The default pass deliberately leaves root login + password auth enabled so you
don't lock yourself out. Once you've verified Tailscale SSH works, re-run with
`--harden` to disable both.

## Inputs

| Env var             | Required          | Notes |
|---------------------|-------------------|-------|
| `NODE_NAME`         | yes               | Cross-node-convention name (`us-ny1`, `us-tx1`, `nl1`). Sets system + Tailscale hostname. Lowercase, digits, hyphens only. |
| `DEPLOY_PUBKEY`     | yes               | Full SSH public key string. Public half of the `DEPLOY_SSH_KEY` GitHub secret used by CI. |
| `TAILSCALE_AUTHKEY` | first run only    | Generate at https://login.tailscale.com/admin/settings/keys (reusable or one-shot, tagged `tag:server` if you have ACLs). |
| `DEPLOY_USER`       | optional          | Default `frionode` — keep this unless you have a strong reason. |
| `SWAP_GB`           | optional          | Default `2`. Skipped if any swap is already configured. |

## Run order

```bash
# On your laptop:
scp infra/bootstrap/bootstrap.sh root@<node-public-ip>:/root/

# On the node, as root:
export NODE_NAME="us-ny1"
export DEPLOY_PUBKEY="ssh-ed25519 AAAA... aaron@deploy"
export TAILSCALE_AUTHKEY="tskey-auth-..."
bash /root/bootstrap.sh
```

Verify from your laptop:

```bash
tailscale ssh frionode@<node-name>      # Tailscale SSH (works even after harden)
ssh frionode@<node-public-ip>           # plain SSH with the deploy key
```

If both work, harden:

```bash
ssh root@<node-public-ip> bash /root/bootstrap.sh --harden
# or:  tailscale ssh root@<node-name> bash /root/bootstrap.sh --harden
```

After harden, the node is ready for `ansible-playbook deploy.yml -i inventories/<node>/hosts.yml`.

## What it deliberately does NOT do

- **Doesn't change SSH port** — Cloudflare/Tailscale fronts the public surface; obscurity isn't worth the operational pain.
- **Doesn't install fail2ban** — UFW + key-only auth is enough for now; revisit if logs show abuse.
- **Doesn't set hostname** — usually already correct from VPS provisioning. Override with `HOSTNAME_OVERRIDE` if needed.
- **Doesn't bind SSH to Tailscale-only** — keeping public SSH (key-only) gives a fallback if Tailscale auth ever breaks.

## Why this lives in zeitghost

This script is project-agnostic — it brings up an Ubuntu node ready for any of
`frio` / `perseus-news` / `zeitghost` to deploy onto. It lives here for now
because zeitghost is the active migration. If frio or perseus need it, lift
into a shared `node-bootstrap` repo and reference from each project's infra/.
