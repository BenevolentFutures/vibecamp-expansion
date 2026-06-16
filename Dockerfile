FROM python:3.11-slim

WORKDIR /app

# Install deps first for layer caching.
COPY pyproject.toml README.md ./
COPY vibecamp_expansion ./vibecamp_expansion
RUN pip install --no-cache-dir .

# Cache + static exports live here; mount a volume for persistent history.
ENV VIBECAMP_DATA_DIR=/data \
    VIBECAMP_CRAWL_INTERVAL=300 \
    PORT=8787
RUN mkdir -p /data

EXPOSE 8787

# One process serves REST + static exports + remote MCP, and runs the crawler
# loop in a background thread. Honors the host-provided $PORT.
CMD ["sh", "-c", "uvicorn vibecamp_expansion.asgi:app --host 0.0.0.0 --port ${PORT:-8787}"]
