#!/bin/bash
# EC2 user-data: runs ONCE at instance launch (Amazon Linux 2023, ARM).
# Sets up an unprivileged service user, writes the deploy + systemd unit
# files, and starts the service. All logs land in /var/log/user-data.log
# and `journalctl -u dictapi` for troubleshooting after launch.
#
# BEFORE pasting this into the console's "User data" field: replace
# BUCKET_NAME below with your real S3 bucket name.
set -eux
exec > >(tee /var/log/user-data.log) 2>&1

BUCKET_NAME="gloss-dict"
REGION="ap-south-1"
APP_DIR="/opt/dictapi"

# AWS CLI v2 ships preinstalled on Amazon Linux 2023; this is a defensive
# fallback in case that ever changes.
command -v aws >/dev/null 2>&1 || dnf install -y awscli

id -u dictapi >/dev/null 2>&1 || useradd --system --no-create-home --shell /sbin/nologin dictapi

mkdir -p "$APP_DIR/bin" "$APP_DIR/data"
chown -R dictapi:dictapi "$APP_DIR"

# ExecStartPre: this is the ENTIRE deploy mechanism. It runs every time the
# service starts or restarts, so `systemctl restart dictapi` = "fetch and
# run whatever CURRENT points at right now" -- there is no separate deploy
# step on the instance.
cat > "$APP_DIR/deploy.sh" <<DEPLOY_EOF
#!/bin/bash
set -euo pipefail
BUCKET="$BUCKET_NAME"
REGION="$REGION"
APP_DIR="$APP_DIR"

aws s3 cp "s3://\$BUCKET/app/dictapi-server" "\$APP_DIR/bin/dictapi-server.new" --region "\$REGION"
chmod +x "\$APP_DIR/bin/dictapi-server.new"
mv "\$APP_DIR/bin/dictapi-server.new" "\$APP_DIR/bin/dictapi-server"

VERSION=\$(aws s3 cp "s3://\$BUCKET/CURRENT" - --region "\$REGION")
echo "Deploying build version: \$VERSION"

aws s3 cp "s3://\$BUCKET/builds/\$VERSION/dict.idx" "\$APP_DIR/data/dict.idx.new" --region "\$REGION"
aws s3 cp "s3://\$BUCKET/builds/\$VERSION/dict.bin" "\$APP_DIR/data/dict.bin.new" --region "\$REGION"
mv "\$APP_DIR/data/dict.idx.new" "\$APP_DIR/data/dict.idx"
mv "\$APP_DIR/data/dict.bin.new" "\$APP_DIR/data/dict.bin"
DEPLOY_EOF
chmod +x "$APP_DIR/deploy.sh"
chown dictapi:dictapi "$APP_DIR/deploy.sh"

cat > /etc/systemd/system/dictapi.service <<'UNIT_EOF'
[Unit]
Description=Dictionary API server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=dictapi
Group=dictapi
ExecStartPre=/opt/dictapi/deploy.sh
ExecStart=/opt/dictapi/bin/dictapi-server
Environment=DICT_DIR=/opt/dictapi/data
Environment=PORT=8080
# TODO before real students use this: replace * with your actual frontend
# origin (e.g. https://your-platform.example), then re-run publish.sh's
# systemctl restart step. * is fine for the smoke test in this guide.
Environment=ALLOW_ORIGIN=*
Restart=on-failure
RestartSec=5
TimeoutStartSec=600
# ExecStartPre downloads ~550MB from S3 -- give it real time on first boot
# rather than letting systemd declare a false-positive startup failure.

[Install]
WantedBy=multi-user.target
UNIT_EOF

systemctl daemon-reload
systemctl enable --now dictapi

echo "user-data complete. Check status with: systemctl status dictapi"
