#!/usr/bin/env bash
# Idempotent installer for twitch-recorder. Safe to re-run: it never
# overwrites an existing config, credentials file or .uploaded.json state.
#
# Usage: sudo ./deploy/install.sh
#
# Override defaults via environment variables, e.g.:
#   INSTALL_DIR=/opt/twitch-recorder CONFIG_DIR=/etc/twitch-recorder sudo -E ./deploy/install.sh
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/twitch-recorder}"
CONFIG_DIR="${CONFIG_DIR:-/etc/twitch-recorder}"
DATA_DIR="${DATA_DIR:-/var/lib/twitch-recorder}"
SERVICE_USER="${SERVICE_USER:-twitch-recorder}"
SERVICE_NAME="${SERVICE_NAME:-twitch-recorder}"

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Installing twitch-recorder"
echo "    source:      $SOURCE_DIR"
echo "    install dir: $INSTALL_DIR"
echo "    config dir:  $CONFIG_DIR"
echo "    data dir:    $DATA_DIR"
echo "    unit user:   $SERVICE_USER"

if [[ "${EUID}" -ne 0 ]]; then
  echo "This script must be run as root (it writes to /opt, /etc and systemd)." >&2
  exit 1
fi

for bin in python3 streamlink ffmpeg ffprobe rclone; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "WARNING: '$bin' is not on PATH. Install it before starting the service." >&2
  fi
done

echo "==> Creating service user '$SERVICE_USER' (if missing)"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA_DIR" --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
else
  echo "    user already exists, skipping"
fi

echo "==> Creating directories"
install -d -m 755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$INSTALL_DIR"
install -d -m 750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CONFIG_DIR"
install -d -m 750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR" "$DATA_DIR/downloads"

echo "==> Copying application files to $INSTALL_DIR"
install -m 644 -o "$SERVICE_USER" -g "$SERVICE_USER" "$SOURCE_DIR/twitch_recorder.py" "$INSTALL_DIR/twitch_recorder.py"
install -m 644 -o "$SERVICE_USER" -g "$SERVICE_USER" "$SOURCE_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"

echo "==> Creating/updating virtualenv at $INSTALL_DIR/.venv"
if [[ ! -d "$INSTALL_DIR/.venv" ]]; then
  python3 -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip --quiet
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" --quiet
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/.venv"

echo "==> Installing config from example (if not already present)"
if [[ -f "$CONFIG_DIR/config.yaml" ]]; then
  echo "    $CONFIG_DIR/config.yaml already exists, leaving it untouched"
else
  install -m 640 -o "$SERVICE_USER" -g "$SERVICE_USER" "$SOURCE_DIR/config.example.yaml" "$CONFIG_DIR/config.yaml"
  echo "    wrote $CONFIG_DIR/config.yaml from config.example.yaml - edit it before starting the service"
fi

echo "==> Installing credentials file from example (if not already present)"
if [[ -f "$CONFIG_DIR/twitch-credentials.yaml" ]]; then
  echo "    $CONFIG_DIR/twitch-credentials.yaml already exists, leaving it untouched"
else
  install -m 600 -o "$SERVICE_USER" -g "$SERVICE_USER" "$SOURCE_DIR/credentials.example.yaml" "$CONFIG_DIR/twitch-credentials.yaml"
  echo "    wrote $CONFIG_DIR/twitch-credentials.yaml from credentials.example.yaml - fill in real secrets"
fi

echo "==> Installing systemd unit"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
sed \
  -e "s#/opt/twitch-recorder#${INSTALL_DIR}#g" \
  -e "s#/etc/twitch-recorder#${CONFIG_DIR}#g" \
  -e "s#/var/lib/twitch-recorder#${DATA_DIR}#g" \
  -e "s#^User=twitch-recorder#User=${SERVICE_USER}#" \
  -e "s#^Group=twitch-recorder#Group=${SERVICE_USER}#" \
  "$SOURCE_DIR/deploy/twitch-recorder.service" > "$UNIT_PATH"
chmod 644 "$UNIT_PATH"

echo "==> Reloading systemd"
systemctl daemon-reload

echo "==> Done."
echo
echo "Next steps:"
echo "  1. Edit $CONFIG_DIR/config.yaml and $CONFIG_DIR/twitch-credentials.yaml"
echo "  2. Validate: sudo -u $SERVICE_USER $INSTALL_DIR/.venv/bin/python $INSTALL_DIR/twitch_recorder.py --config $CONFIG_DIR/config.yaml --check-config"
echo "  3. Start:    systemctl enable --now ${SERVICE_NAME}"
