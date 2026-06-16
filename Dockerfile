FROM python:3.11-slim

WORKDIR /app

# Install deps first for layer caching.
COPY pyproject.toml README.md ./
COPY vibecamp_expansion ./vibecamp_expansion
# tzdata lets $TZ resolve so the bots reckon "next event" in festival-local
# wall-clock time. Install the bot extras too: the same image backs the web
# service and the Discord/Telegram worker services (each runs a different CMD).
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir ".[discord,telegram]"

# Cache + static exports live here; mount a volume for persistent history.
ENV VIBECAMP_DATA_DIR=/data \
    VIBECAMP_CRAWL_INTERVAL=300 \
    PORT=8787
RUN mkdir -p /data

EXPOSE 8787

# One image, three roles — selected by $VIBECAMP_ROLE so each Railway service
# is just this image plus an env var (no per-service start-command needed):
#   web (default) -> REST + static + remote MCP + crawler thread (honors $PORT)
#   discord       -> the Discord bot   (needs $DISCORD_BOT_TOKEN)
#   telegram      -> the Telegram bot  (needs $TELEGRAM_BOT_TOKEN)
CMD ["sh", "-c", "case \"${VIBECAMP_ROLE:-web}\" in \
  discord) exec vibecamp discord ;; \
  telegram) exec vibecamp telegram ;; \
  *) exec uvicorn vibecamp_expansion.asgi:app --host 0.0.0.0 --port ${PORT:-8787} \
       --proxy-headers --forwarded-allow-ips='*' ;; \
esac"]
