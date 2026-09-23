#!/usr/bin/env bash
# Runs on your laptop. Rolls back to a previous build version by flipping
# CURRENT in S3, then tells you the exact command to run on the instance.
# Old builds are never deleted by publish.sh, so any version that was ever
# published can be rolled back to.
#
# Usage:
#   ./rollback.sh                    # list available build versions
#   ./rollback.sh 1790073003         # roll back to that version
set -euo pipefail

BUCKET="${BUCKET:-CHANGE-ME-dictapi-bucket}"
REGION="${REGION:-ap-south-1}"

if [ "$BUCKET" = "CHANGE-ME-dictapi-bucket" ]; then
  echo "Set BUCKET first: BUCKET=your-bucket ./rollback.sh [version]" >&2
  exit 1
fi

if [ $# -eq 0 ]; then
  echo "Current: $(aws s3 cp "s3://$BUCKET/CURRENT" - --region "$REGION")"
  echo
  echo "Available build versions:"
  aws s3 ls "s3://$BUCKET/builds/" --region "$REGION" | awk '{print $2}' | sed 's#/$##'
  echo
  echo "Usage: ./rollback.sh <version>"
  exit 0
fi

VERSION="$1"

if ! aws s3api head-object --bucket "$BUCKET" --key "builds/$VERSION/dict.bin" --region "$REGION" >/dev/null 2>&1; then
  echo "builds/$VERSION/dict.bin not found in s3://$BUCKET -- typo, or that version was never published?" >&2
  exit 1
fi

echo "==> Rolling CURRENT back to $VERSION ..."
echo -n "$VERSION" | aws s3 cp - "s3://$BUCKET/CURRENT" --region "$REGION"

echo
echo "CURRENT now points at $VERSION. Apply it to the running instance with:"
echo "  ssh ec2-user@<elastic-ip> 'sudo systemctl restart dictapi'"
echo
echo "Verify afterward: curl -sI https://<cloudfront-domain>/v1/define/went | grep etag"
