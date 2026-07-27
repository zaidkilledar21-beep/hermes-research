#!/usr/bin/env bash
# Hermes Research Engine — Phase 1 VPS foundation.
# Run ON THE VPS as a sudo-capable user:  sudo bash 01-foundation.sh
# Idempotent: safe to re-run. Does NOT install Hermes or any app — foundation only.
#
# Reconciliation note: the plan calls for separate `hermes`/`reach` OS users. The real
# security boundary (per Hermes's own SECURITY.md) is the CONTAINER, not the host user.
# So host users here exist only to OWN mounted directories with clean, distinct perms;
# actual isolation is enforced in docker-compose (separate networks, no shared secrets,
# one-way dropbox). Neither user is in the docker group — compose is run with sudo.

set -euo pipefail

echo "== 0. Preflight =="
if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo."; exit 1; fi
. /etc/os-release
echo "OS: $PRETTY_NAME   RAM: $(free -h | awk '/Mem:/{print $2}')   CPU: $(nproc) cores"

echo "== 1. Swapfile (box ships with 0B swap; 2GB insurance against OOM when reach Chromium spikes) =="
if ! swapon --show | grep -q '/swapfile'; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  # low swappiness: prefer RAM, use swap only as a safety net
  sysctl -w vm.swappiness=10
  grep -q 'vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf
  echo "swapfile created."
else
  echo "swapfile already present, skipping."
fi

echo "== 2. Base packages =="
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg jq git ufw fail2ban >/dev/null
echo "base packages ok."

echo "== 3. Docker Engine + compose plugin (official repo) =="
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
  systemctl enable --now docker
  echo "docker installed: $(docker --version)"
else
  echo "docker already present: $(docker --version), skipping."
fi

echo "== 4. Host users (dir-owners only; NOT in docker group) =="
id hermes >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin hermes
id reach  >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin reach
echo "users ok."

echo "== 5. Directory structure =="
mkdir -p /opt/hermes/{config,secrets,evidence,skills,collectors,logs}
mkdir -p /opt/reach/{app,secrets,logs}
mkdir -p /opt/dropbox   # reach WRITES here; hermes reads it (read-only mount in compose)
# ownership: hermes owns its tree, reach owns its tree + the dropbox (it's the writer)
chown -R hermes:hermes /opt/hermes
chown -R reach:reach  /opt/reach /opt/dropbox
# secrets dirs locked down
chmod 700 /opt/hermes/secrets /opt/reach/secrets
# evidence store append-only-ish: hermes writes, world cannot
chmod 750 /opt/hermes/evidence
chmod 770 /opt/dropbox
echo "dirs ok."

echo "== 6. Firewall (SSH only; dashboard/gateway never public) =="
ufw allow OpenSSH >/dev/null
ufw --force enable >/dev/null
ufw status verbose | head -5

echo "== 7. Reboot check =="
if [ -f /var/run/reboot-required ]; then
  echo "!! REBOOT PENDING (kernel/libs from apt upgrade). Reboot before Phase 2:  sudo reboot"
else
  echo "no reboot flag."
fi

echo "== DONE — Phase 1 foundation complete. =="
echo "Next: deploy/hermes-kill.sh installed separately; then Phase 2 (Hermes container)."
