#!/bin/bash
# Grocy+ container entrypoint (Railway).
# - seeds the data volume on first start and creates config.php
# - clears the view cache
# - makes Apache listen on $PORT
# - requests "/" once in the background so Grocy runs its DB migrations
set -euo pipefail

APP_DIR=/var/www/html
DATA_DIR="$APP_DIR/data"
PRISTINE=/opt/grocy-data-pristine
PORT="${PORT:-8080}"
export PORT

mkdir -p "$DATA_DIR"

# Seed the (possibly empty) volume from the image's pristine data/ directory
if [ ! -d "$DATA_DIR/plugins" ]; then
	echo "[grocy+] Seeding data volume from image"
	cp -an "$PRISTINE/." "$DATA_DIR/"
fi
mkdir -p "$DATA_DIR/storage" "$DATA_DIR/viewcache" "$DATA_DIR/plugins"

# config.php must exist; GROCY_* env vars override anything in it
if [ ! -f "$DATA_DIR/config.php" ]; then
	echo "[grocy+] Creating data/config.php from config-dist.php"
	cp "$APP_DIR/config-dist.php" "$DATA_DIR/config.php"
	# Food is usually used partially: an opened pack must still count as in stock
	sed -i "s/DefaultUserSetting('product_presets_treat_opened_as_out_of_stock', true)/DefaultUserSetting('product_presets_treat_opened_as_out_of_stock', false)/" "$DATA_DIR/config.php"
fi

# The Grocy+ design ("Night Pantry"): Grocy includes data/custom_css.html and custom_js.html on every
# page. Ours are refreshed on every start; files written by hand are kept.
# GROCY_PLUS_THEME=off removes ours and shows Grocy's original look.
for f in custom_css.html custom_js.html; do
	dest="$DATA_DIR/$f"
	if [ -e "$dest" ] && ! grep -q "grocy-plus-theme" "$dest"; then
		echo "[grocy+] Keeping existing data/$f (not ours)"
	elif [ "${GROCY_PLUS_THEME:-night}" = "off" ]; then
		rm -f "$dest"
	else
		cp "$APP_DIR/deploy/grocy/theme/$f" "$dest"
	fi
done
echo "[grocy+] Design: ${GROCY_PLUS_THEME:-night}"

# Before a new Grocy version migrates the database, keep a full snapshot of the old state.
# (Apache isn't running yet, so nothing can trigger the migrations before this.)
version="$(php -r 'echo json_decode(file_get_contents("/var/www/html/version.json"))->Version;')"
last_version="$(cat "$DATA_DIR/.grocy_plus_version" 2>/dev/null || true)"
if [ -f "$DATA_DIR/grocy.db" ] && [ "$version" != "$last_version" ]; then
	echo "[grocy+] Grocy ${last_version:-unknown} -> $version: snapshot before migrations"
	grocy-backup --full "pre-upgrade-${last_version:-unknown}-to-$version"
fi
echo "$version" > "$DATA_DIR/.grocy_plus_version"

# Always start with an empty view cache (also forces Grocy to re-check migrations)
rm -rf "$DATA_DIR/viewcache/"*

chown -R www-data:www-data "$DATA_DIR"

# mod_php needs exactly one MPM, prefork ("More than one MPM loaded" otherwise)
for mpm in event worker; do
	rm -f "/etc/apache2/mods-enabled/mpm_${mpm}.load" "/etc/apache2/mods-enabled/mpm_${mpm}.conf"
done
[ -e /etc/apache2/mods-enabled/mpm_prefork.load ] || a2enmod -q mpm_prefork

# Apache on Railway's $PORT
echo "Listen ${PORT}" > /etc/apache2/ports.conf

# Trigger DB migrations: they only run when "/" is requested
(
	for i in $(seq 1 120); do
		code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/" || true)
		if [ "$code" != "000" ]; then
			echo "[grocy+] Requested / (HTTP $code), migrations done"
			# Second request: the first one may only have redirected after cache invalidation
			curl -s -o /dev/null "http://127.0.0.1:${PORT}/" || true
			exit 0
		fi
		sleep 1
	done
	echo "[grocy+] WARNING: Grocy did not answer on / within 120 s"
) &

# Daily database snapshot at GROCY_BACKUP_HOUR (default 03, container TZ); GROCY_BACKUP_DAILY=false disables
if [ "${GROCY_BACKUP_DAILY:-true}" = "true" ]; then
	(
		while true; do
			now=$(date +%s)
			next=$(date -d "today ${GROCY_BACKUP_HOUR:-03}:00" +%s)
			[ "$next" -le "$now" ] && next=$(date -d "tomorrow ${GROCY_BACKUP_HOUR:-03}:00" +%s)
			sleep $((next - now))
			grocy-backup daily || echo "[grocy+] WARNING: daily backup failed"
		done
	) &
fi

exec "$@"
