#!/usr/bin/env bash
set -euo pipefail
APP_DIR="${APP_DIR:-/opt/ntdrevizor}"
DEST="${1:-/var/backups/ntdrevizor}"
mkdir -p "$DEST"
STAMP=$(date +%Y%m%d_%H%M%S)
tar -C "$APP_DIR" -czf "$DEST/ntdrevizor_$STAMP.tgz" \
  data/app.db data/secret.key .env data/uploads data/ntd_catalog.json 2>/dev/null || \
tar -C "$APP_DIR" -czf "$DEST/ntdrevizor_$STAMP.tgz" data .env
echo "Создано $DEST/ntdrevizor_$STAMP.tgz"
