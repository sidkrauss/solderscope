#!/usr/bin/env bash
# Update MediaMTX on the Pi, keeping /etc/mediamtx.yml.
#
#   ./scripts/update-mediamtx.sh [user@host] [version]
#
# Config keys occasionally change between MediaMTX releases, so the new binary
# is validated against the existing config BEFORE it replaces the running one.
# On failure the old binary stays in place and the service is never touched.
set -euo pipefail

TARGET="${1:-${SOLDERSCOPE_TARGET:-master@solderscope.local}}"
VERSION="${2:-}"

if [[ -z "$VERSION" ]]; then
  VERSION="$(curl -fsSL https://api.github.com/repos/bluenviron/mediamtx/releases/latest \
             | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p')"
fi
[[ -n "$VERSION" ]] || { echo "Could not determine the latest version" >&2; exit 1; }

echo "==> Target $TARGET, version $VERSION"

ssh "$TARGET" "
set -euo pipefail

if pgrep -x apt >/dev/null || pgrep -x apt-get >/dev/null || pgrep -x dpkg >/dev/null; then
  echo 'ERROR: apt/dpkg is running. Wait for it to finish.' >&2; exit 1
fi

current=\$(/usr/local/bin/mediamtx --version 2>/dev/null || echo unknown)
echo \"   installed: \$current\"
if [ \"\$current\" = \"$VERSION\" ]; then echo '   already up to date.'; exit 0; fi

tmp=\$(mktemp -d); trap 'rm -rf \$tmp' EXIT
echo '   downloading...'
curl -fsSL -o \$tmp/m.tar.gz \
  https://github.com/bluenviron/mediamtx/releases/download/$VERSION/mediamtx_${VERSION}_linux_arm64.tar.gz
tar -xzf \$tmp/m.tar.gz -C \$tmp

echo '   validating the existing config against the new version...'
# Start the new binary briefly on throwaway ports; if it parses the config and
# stays alive, the config is compatible.
# Every listener must move, TCP and UDP alike -- a single missed port makes
# the probe fail with \"address already in use\" and looks like a config error.
sed -e 's/^rtspAddress:.*/rtspAddress: :18554/' \
    -e 's/^rtspsAddress:.*/rtspsAddress: :18322/' \
    -e 's/^hlsAddress:.*/hlsAddress: :18888/' \
    -e 's/^webrtcAddress:.*/webrtcAddress: :18889/' \
    -e 's/^rtmpAddress:.*/rtmpAddress: :11935/' \
    -e 's/^rtmpsAddress:.*/rtmpsAddress: :11936/' \
    -e 's/^apiAddress:.*/apiAddress: :19997/' \
    -e 's/^metricsAddress:.*/metricsAddress: :19998/' \
    -e 's/^pprofAddress:.*/pprofAddress: :19999/' \
    -e 's/^playbackAddress:.*/playbackAddress: :19996/' \
    -e 's/^srtAddress:.*/srtAddress: :18890/' \
    -e 's/^rtpAddress:.*/rtpAddress: :18000/' \
    -e 's/^rtcpAddress:.*/rtcpAddress: :18001/' \
    -e 's/^multicastRTPPort:.*/multicastRTPPort: 18002/' \
    -e 's/^multicastRTCPPort:.*/multicastRTCPPort: 18003/' \
    -e 's/^srtpAddress:.*/srtpAddress: :18004/' \
    -e 's/^srtcpAddress:.*/srtcpAddress: :18005/' \
    -e 's/^webrtcLocalUDPAddress:.*/webrtcLocalUDPAddress: :18189/' \
    -e 's/^rtspAuthMethods:.*/rtspAuthMethods: [basic]/' \
    /etc/mediamtx.yml > \$tmp/test.yml
# Drop the camera path: the sensor is busy and would fail for unrelated reasons.
sed -i 's/^    source: rpiCamera/    source: publisher/' \$tmp/test.yml

\$tmp/mediamtx \$tmp/test.yml >\$tmp/out.log 2>&1 &
pid=\$!
sleep 6
if kill -0 \$pid 2>/dev/null; then
  kill \$pid 2>/dev/null; wait \$pid 2>/dev/null || true
  echo '   config is compatible.'
else
  echo '   ERROR: the new version rejects the config:' >&2
  tail -15 \$tmp/out.log >&2
  echo '   Update aborted; the previous version stays active.' >&2
  exit 1
fi

echo '   installing...'
sudo cp /usr/local/bin/mediamtx /usr/local/bin/mediamtx.bak-\$(date +%F)
sudo install -m 0755 \$tmp/mediamtx /usr/local/bin/mediamtx
sudo systemctl restart mediamtx
sleep 8
systemctl is-active --quiet mediamtx && echo \"   running: \$(/usr/local/bin/mediamtx --version)\" || {
  echo '   Startup failed, rolling back' >&2
  sudo cp /usr/local/bin/mediamtx.bak-\$(date +%F) /usr/local/bin/mediamtx
  sudo systemctl restart mediamtx
  exit 1
}
"

echo "==> Stream check"
HOST="${TARGET#*@}"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 "http://$HOST:8888/cam/index.m3u8" || true)
[[ "$code" == "200" ]] && echo "   HLS OK ($code)" || echo "   WARNING: HLS returned $code"
