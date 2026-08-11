# syntax=docker/dockerfile:1
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY catalog.csv ./catalog.csv

# The SQLite database lives here. Mount the single Railway volume at /app/data
# and set PERSISTENT_VOLUME_PATH=/app/data for production startup validation.
RUN mkdir -p /app/data
ENV ORDERS_DB_PATH=/app/data/orders.db

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status==200 else 1)"

# FORWARDED_ALLOW_IPS defaults to loopback for local use only. Railway
# production must configure the documented explicit proxy CIDRs, and must run
# exactly one Uvicorn worker against exactly one mounted SQLite volume.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-8000}\" --workers 1 --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]
