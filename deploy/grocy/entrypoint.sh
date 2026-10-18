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

# Always start with an empty view cache (also forces Grocy to re-check migrations)
rm -rf "$DATA_DIR/viewcache/"*

chown -R www-data:www-data "$DATA_DIR"

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

exec "$@"
