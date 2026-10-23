# Grocy+ companion. Build context = companion/  (Railway service root directory: /companion)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
	PYTHONUNBUFFERED=1 \
	PIP_NO_CACHE_DIR=1 \
	TZ=Europe/Vilnius \
	COMPANION_DB_PATH=/data/companion.db \
	PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY static ./static

# Runs as root: Railway mounts volumes owned by root, so a non-root user could not write /data
RUN mkdir -p /data
# Data lives in /data: attach a Railway volume there (Railway rejects the VOLUME instruction)
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
