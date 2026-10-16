#!/usr/bin/env bash
# Nightly scorecard backup: consistent SQLite snapshot out of the container,
# verified with PRAGMA integrity_check, 14-day rotation. Install: crontab -e ->
#   15 2 * * * /home/USER/scorecard/deploy/backup.sh >> /home/USER/scorecard-backups/backup.log 2>&1
#
# Optional environment (set them on the crontab line, before the command):
#   BACKUP_DIR     local destination (default: $HOME/scorecard-backups)
#   BACKUP_REMOTE  also copy each verified snapshot OFF this host, as an
#                  rsync/scp target ending in "/", e.g.
#                  BACKUP_REMOTE=backup@backup.example.com:/srv/backups/scorecard/
#                  Uses rsync when installed, else scp; needs key-based SSH
#                  (BatchMode - cron cannot type a password). The remote side
#                  is not rotated; prune it there.
#
# A failed snapshot, a failed integrity check or a failed off-host copy exits
# non-zero with "backup FAILED" on stderr, and never touches the backups
# already on disk: rotation runs only after a new good one is in place.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${BACKUP_DIR:-$HOME/scorecard-backups}"
STAMP=$(date +%Y%m%d-%H%M)
NAME="scorecard-$STAMP.db.gz"
TMP_IN=/srv/scorecard/data/.backup-tmp.db      # inside the container's volume
PARTIAL="$DEST/.scorecard-$STAMP.partial"      # host side, until verified
mkdir -p "$DEST"

ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }      # portable; GNU-only date -Is is not
fail() { echo "$(ts) backup FAILED: $*" >&2; exit 1; }
cleanup() {
    rm -f "$PARTIAL" "$PARTIAL.gz"
    docker compose exec -T scorecard rm -f "$TMP_IN" >/dev/null 2>&1 || true
}
trap cleanup EXIT

cd "$APP_DIR"
# Online backup API: a consistent copy even while the app writes under WAL
# (copying scorecard.db by hand can miss everything still in -wal). The copy
# is switched to rollback-journal mode so it is ONE self-contained file, then
# checked before it is allowed anywhere near the backup folder.
docker compose exec -T scorecard python -c "
import sqlite3, sys
src = sqlite3.connect('/srv/scorecard/data/scorecard.db')
dst = sqlite3.connect('$TMP_IN')
src.backup(dst)
src.close()
dst.execute('PRAGMA journal_mode = DELETE')
res = [r[0] for r in dst.execute('PRAGMA integrity_check')]
dst.close()
if res != ['ok']:
    print('integrity_check: ' + '; '.join(res[:10])[:2000], file=sys.stderr)
    sys.exit(1)
" || fail "snapshot or integrity_check failed (previous backups untouched)"

docker compose cp "scorecard:$TMP_IN" "$PARTIAL" || fail "could not copy the snapshot out"
gzip -f "$PARTIAL" && gzip -t "$PARTIAL.gz" || fail "compression check failed"
mv "$PARTIAL.gz" "$DEST/$NAME"
echo "$(ts) backup ok: $NAME ($(du -h "$DEST/$NAME" | cut -f1))"

# Rotate: keep 14 days. Only reached once today's backup is verified.
find "$DEST" -name "scorecard-*.db.gz" -mtime +14 -delete

if [ -n "${BACKUP_REMOTE:-}" ]; then
    if command -v rsync >/dev/null 2>&1; then
        rsync -a -e "ssh -o BatchMode=yes" "$DEST/$NAME" "$BACKUP_REMOTE" \
            || fail "off-host copy to BACKUP_REMOTE failed (local backup kept)"
    else
        scp -q -o BatchMode=yes "$DEST/$NAME" "$BACKUP_REMOTE" \
            || fail "off-host copy to BACKUP_REMOTE failed (local backup kept)"
    fi
    echo "$(ts) copied off-host: $NAME"
fi
