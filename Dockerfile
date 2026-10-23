# Grocy+ : upstream Grocy built from this fork, for Railway.
# Build context = repository root:  docker build .
# (At the repo root so Railway always builds with it instead of auto-detecting PHP with Railpack.)

# --- Frontend dependencies (yarn writes to public/packages via .yarnrc) ---
# node:24, not 22: @zxing/library 0.23 declares engines.node >= 24
FROM node:24 AS frontend
WORKDIR /src
COPY package.json yarn.lock .yarnrc ./
RUN yarn install --frozen-lockfile --network-timeout 600000 \
	&& rm -rf /usr/local/share/.cache/yarn

# --- PHP dependencies (vendor-dir = packages/) ---
FROM composer:2 AS backend
WORKDIR /src
COPY composer.json composer.lock ./
RUN composer install --no-dev --ignore-platform-reqs --no-scripts --no-interaction --no-progress --no-autoloader
COPY services ./services
COPY controllers ./controllers
COPY middleware ./middleware
COPY helpers ./helpers
RUN composer dump-autoload --no-dev --optimize --ignore-platform-reqs

# --- Runtime ---
FROM php:8.5-apache

RUN apt-get update \
	&& apt-get install -y --no-install-recommends \
		libfreetype6-dev libjpeg62-turbo-dev libpng-dev libwebp-dev libicu-dev curl \
	&& docker-php-ext-configure gd --with-freetype --with-jpeg --with-webp \
	&& docker-php-ext-install -j"$(nproc)" gd intl \
	&& a2enmod rewrite \
	&& (a2dismod -f mpm_event mpm_worker || true) && a2enmod mpm_prefork \
	&& echo "ServerName localhost" > /etc/apache2/conf-available/servername.conf && a2enconf servername \
	&& rm -rf /var/lib/apt/lists/*

COPY deploy/grocy/php.ini /usr/local/etc/php/conf.d/grocy.ini
COPY deploy/grocy/apache-grocy.conf /etc/apache2/sites-available/000-default.conf

WORKDIR /var/www/html
COPY --chown=www-data:www-data . .
COPY --from=backend --chown=www-data:www-data /src/packages ./packages
COPY --from=frontend --chown=www-data:www-data /src/public/packages ./public/packages

# Pristine data/ used to seed the Railway volume on first start
RUN cp -a data /opt/grocy-data-pristine \
	&& install -m 0755 deploy/grocy/entrypoint.sh /usr/local/bin/grocy-entrypoint \
	&& install -m 0755 deploy/grocy/backup.sh /usr/local/bin/grocy-backup

ENV PORT=8080 \
	GROCY_CURRENCY=EUR \
	GROCY_STOCK_BARCODE_LOOKUP_PLUGIN=OpenFoodFactsBarcodeLookupPlugin \
	GROCY_FEATURE_FLAG_STOCK_PRODUCT_OPENED_TRACKING=true \
	GROCY_ENERGY_UNIT=kcal \
	TZ=Europe/Vilnius

# Data lives in /var/www/html/data: attach a Railway volume there (Railway rejects the VOLUME instruction)
EXPOSE 8080
ENTRYPOINT ["grocy-entrypoint"]
CMD ["apache2-foreground"]
