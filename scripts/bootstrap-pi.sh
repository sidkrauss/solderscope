#!/usr/bin/env bash
# Prepare a fresh Raspberry Pi OS (Bookworm, 64-bit) for solderscope.
# Run ON THE PI, once. Afterwards use deploy.sh from the workstation.
#
# Assumes: Pi Zero 2 W + IMX477 (HQ Camera), headless install.
set -euo pipefail

MTX_VERSION="${MTX_VERSION:-v1.19.3}"
ARCH="linux_arm64"

echo "==> Packages"
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  python3 python3-pil rpicam-apps ffmpeg curl rsync

echo "==> MediaMTX ${MTX_VERSION}"
if ! command -v mediamtx >/dev/null; then
  tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/m.tar.gz" \
    "https://github.com/bluenviron/mediamtx/releases/download/${MTX_VERSION}/mediamtx_${MTX_VERSION}_${ARCH}.tar.gz"
  tar -xzf "$tmp/m.tar.gz" -C "$tmp"
  sudo install -m 0755 "$tmp/mediamtx" /usr/local/bin/mediamtx
  sudo cp "$tmp/mediamtx.yml" /etc/mediamtx.yml
  rm -rf "$tmp"
fi

echo "==> mediamtx.service"
sudo tee /etc/systemd/system/mediamtx.service >/dev/null <<'UNIT'
[Unit]
Description=MediaMTX (RTSP/WebRTC/HLS)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/mediamtx /etc/mediamtx.yml
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNIT

echo "==> Headless: no desktop"
sudo systemctl set-default multi-user.target
sudo systemctl disable lightdm 2>/dev/null || true

echo "==> Disabling services this box does not need (printing, modem)"
for u in cups cups-browsed ModemManager; do
  sudo systemctl disable --now "$u" 2>/dev/null || true
done

sudo systemctl daemon-reload
sudo systemctl enable --now mediamtx

cat <<'EOF'

Bootstrap complete.

Check the camera:  rpicam-hello --list-cameras
Then, from your workstation:  ./scripts/deploy.sh master@<host>
EOF
