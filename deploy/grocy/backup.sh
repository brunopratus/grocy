#!/bin/bash
# Grocy+ backup: consistent SQLite snapshot (VACUUM INTO, safe while Grocy runs) into data/backups.
#
#   grocy-backup [label]            database only
#   grocy-backup --full [label]     database + storage/ (photos, files) + config.php
#
# Keeps the newest $GROCY_BACKUP_KEEP (default 14) snapshots per kind. Backups live on the same
# volume, so they protect against bad upgrades and mistakes, not against losing the volume:
# use Railway's volume backups for that (see GROCY_PLUS.md).
set -euo pipefail

DATA_DIR=/var/www/html/data
DEST="$DATA_DIR/backups"
KEEP="${GROCY_BACKUP_KEEP:-14}"

full=false
if [ "${1:-}" = "--full" ]; then
	full=true
	shift
fi
label="$(printf '%s' "${1:-manual}" | tr -c 'A-Za-z0-9._-' '_')"

if [ ! -f "$DATA_DIR/grocy.db" ]; then
	echo "[grocy+] backup: no database yet, nothing to do"
	exit 0
fi

mkdir -p "$DEST"
ts="$(date +%Y%m%d-%H%M%S)"
db="$DEST/grocy-$ts-$label.db"

php -r '
	$src = new PDO("sqlite:" . $argv[1]);
	$src->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
	$src->exec("VACUUM INTO " . $src->quote($argv[2]));
' "$DATA_DIR/grocy.db" "$db"
gzip -9 "$db"
echo "[grocy+] backup: $db.gz ($(du -h "$db.gz" | cut -f1))"

if [ "$full" = true ]; then
	files="$DEST/files-$ts-$label.tgz"
	tar -czf "$files" -C "$DATA_DIR" storage config.php
	echo "[grocy+] backup: $files ($(du -h "$files" | cut -f1))"
fi

# Retention
ls -1t "$DEST"/grocy-*.db.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
ls -1t "$DEST"/files-*.tgz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
chown -R www-data:www-data "$DEST"
