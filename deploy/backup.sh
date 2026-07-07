#!/usr/bin/env bash
# Nightly scorecard backup: consistent SQLite snapshot out of the container,
# 14-day rotation. Install: crontab -e ->
#   15 2 * * * /home/USER/scorecard/deploy/backup.sh >> /home/USER/scorecard-backups/backup.log 2>&1
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/scorecard-backups"
STAMP=$(date +%Y%m%d-%H%M)
mkdir -p "$DEST"

cd "$APP_DIR"
# Online backup (safe under WAL) to a temp file inside the volume, then copy out.
docker compose exec -T scorecard python -c "
import sqlite3
src = sqlite3.connect('/srv/scorecard/data/scorecard.db')
dst = sqlite3.connect('/srv/scorecard/data/.backup-tmp.db')
src.backup(dst)
dst.close(); src.close()
"
docker compose cp scorecard:/srv/scorecard/data/.backup-tmp.db "$DEST/scorecard-$STAMP.db"
docker compose exec -T scorecard rm -f /srv/scorecard/data/.backup-tmp.db
gzip -f "$DEST/scorecard-$STAMP.db"

# Rotate: keep 14 days.
find "$DEST" -name "scorecard-*.db.gz" -mtime +14 -delete
echo "$(date -Is) backup ok: scorecard-$STAMP.db.gz ($(du -h "$DEST/scorecard-$STAMP.db.gz" | cut -f1))"
