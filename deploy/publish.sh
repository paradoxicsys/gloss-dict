#!/usr/bin/env bash
# Runs on your laptop. Cross-compiles the server for the EC2 target
# (linux/arm64), uploads it + the current Phase 2 build to S3, then flips
# CURRENT to point at the new build version -- LAST, and only if both data
# files uploaded successfully, so CURRENT never points at a half-uploaded
# build.
#
# Prereqs: `aws configure` already run once with your own credentials
# (these never touch the server -- only used here, on your laptop).
#
# Usage:
#   ./publish.sh                 # upload code + whatever is in dict-api/out/
#   BUCKET=my-bucket ./publish.sh
set -euo pipefail

BUCKET="${BUCKET:-CHANGE-ME-dictapi-bucket}"
REGION="${REGION:-ap-south-1}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$REPO_ROOT/out"
SERVER_DIR="$REPO_ROOT/server"

if [ "$BUCKET" = "CHANGE-ME-dictapi-bucket" ]; then
  echo "Set BUCKET to your real bucket name first: BUCKET=your-bucket ./publish.sh" >&2
  exit 1
fi

if [ ! -f "$OUT_DIR/dict.idx" ] || [ ! -f "$OUT_DIR/dict.bin" ]; then
  echo "No build found at $OUT_DIR -- run the Phase 2 build first:" >&2
  echo "  python3 $REPO_ROOT/build/build.py" >&2
  exit 1
fi

# The build version is already baked into dict.idx's header (it's what the
# server uses as its ETag) -- reuse it as the S3 folder name so there's one
# source of truth, not two version numbers to keep in sync.
BUILD_VERSION=$(python3 - "$OUT_DIR/dict.idx" <<'EOF'
import sys, struct
with open(sys.argv[1], "rb") as f:
    header = f.read(16)
magic, build_version, count = struct.unpack("<4sQI", header)
assert magic == b"DIX1", f"bad magic: {magic!r}"
print(build_version)
EOF
)
echo "==> Build version: $BUILD_VERSION"

echo "==> Cross-compiling server for linux/arm64 ..."
GOOS=linux GOARCH=arm64 go -C "$SERVER_DIR" build -ldflags="-s -w" -o /tmp/dictapi-server-linux-arm64 .
BIN_SIZE=$(ls -la /tmp/dictapi-server-linux-arm64 | awk '{print $5}')
echo "    binary: $BIN_SIZE bytes"

echo "==> Uploading app code (app/dictapi-server) ..."
aws s3 cp /tmp/dictapi-server-linux-arm64 "s3://$BUCKET/app/dictapi-server" --region "$REGION"

BUILD_PREFIX="builds/$BUILD_VERSION"
if aws s3api head-object --bucket "$BUCKET" --key "$BUILD_PREFIX/dict.bin" --region "$REGION" >/dev/null 2>&1; then
  echo "==> builds/$BUILD_VERSION/ already exists in S3 -- skipping the 517MB re-upload."
else
  echo "==> Uploading builds/$BUILD_VERSION/dict.idx + dict.bin (this is the big one, ~550MB) ..."
  aws s3 cp "$OUT_DIR/dict.idx" "s3://$BUCKET/$BUILD_PREFIX/dict.idx" --region "$REGION"
  aws s3 cp "$OUT_DIR/dict.bin" "s3://$BUCKET/$BUILD_PREFIX/dict.bin" --region "$REGION"
fi

echo "==> Flipping CURRENT -> $BUILD_VERSION ..."
echo -n "$BUILD_VERSION" | aws s3 cp - "s3://$BUCKET/CURRENT" --region "$REGION"

echo
echo "Done. To deploy this to a running instance:"
echo "  ssh ec2-user@<elastic-ip> 'sudo systemctl restart dictapi'"
echo
echo "To roll back to a previous build later, see deploy/rollback.sh."
