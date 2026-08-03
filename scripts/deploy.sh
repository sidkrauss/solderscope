#!/usr/bin/env bash
# Deploy solderscope to the Pi. Idempotent -- safe to re-run after changes.
#
#   ./scripts/deploy.sh [user@host]
#
set -euo pipefail

# Override per invocation, or set SOLDERSCOPE_TARGET in your environment.
TARGET="${1:-${SOLDERSCOPE_TARGET:-master@solderscope.local}}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Target: $TARGET"

# Refuse to touch a machine mid-upgrade: restarting mediamtx while dpkg is
# configuring packages is a good way to end up with a half-broken service.
if ssh "$TARGET" 'pgrep -x apt >/dev/null || pgrep -x apt-get >/dev/null || pgrep -x dpkg >/dev/null'; then
  echo "ERROR: apt/dpkg is running on the Pi. Wait for it to finish, then retry." >&2
  exit 1
fi

echo "==> Copying files"
ssh "$TARGET" 'sudo mkdir -p /opt/solderscope && sudo chown -R $USER /opt/solderscope'
rsync -az --delete \
  --exclude '.git' --exclude 'docs' --exclude '*.bak*' \
  "$REPO_DIR/service" "$REPO_DIR/web" "$TARGET:/opt/solderscope/"

echo "==> Media directories"
ssh "$TARGET" 'mkdir -p ~/solderscope-media/photos ~/solderscope-media/recordings'

echo "==> polkit rule (manage mediamtx.service only)"
scp -q "$REPO_DIR/config/50-solderscope.rules" "$TARGET:/tmp/50-solderscope.rules"
ssh "$TARGET" '
  sudo install -m 0644 -o root -g root /tmp/50-solderscope.rules \
    /etc/polkit-1/rules.d/50-solderscope.rules
  rm -f /tmp/50-solderscope.rules
  sudo systemctl restart polkit
  echo "   polkit rule installed"
'

echo "==> MediaMTX configuration"
scp -q "$REPO_DIR/config/mediamtx.yml" "$TARGET:/tmp/mediamtx.yml"
ssh "$TARGET" '
  sudo cp -n /etc/mediamtx.yml /etc/mediamtx.yml.orig 2>/dev/null || true
  sudo cp /tmp/mediamtx.yml /etc/mediamtx.yml
  rm -f /tmp/mediamtx.yml
'

echo "==> systemd unit"
scp -q "$REPO_DIR/service/solderscope.service" "$TARGET:/tmp/solderscope.service"
ssh "$TARGET" '
  sudo mv /tmp/solderscope.service /etc/systemd/system/solderscope.service
  sudo systemctl daemon-reload
  sudo systemctl enable solderscope >/dev/null 2>&1
  sudo systemctl restart mediamtx
  sudo systemctl restart solderscope
'

echo "==> Waiting for services"
sleep 8
ssh "$TARGET" '
  for u in mediamtx solderscope; do
    printf "   %-10s %s\n" "$u" "$(systemctl is-active $u)"
  done
'

HOST="${TARGET#*@}"
echo
echo "Done. Web UI:  http://$HOST"
