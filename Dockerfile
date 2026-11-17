FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/srv/ledgerroute/data \
    OPERATIONAL_DB_PATH=/srv/ledgerroute/var/operational.db \
    ANALYTICS_DB_PATH=/srv/ledgerroute/var/analytics.db

WORKDIR /srv/ledgerroute
COPY pyproject.toml README.md LICENSE ./
COPY app/ ./app/
COPY data/ ./data/
RUN python -m pip install --no-cache-dir . \
    && groupadd --gid 10001 ledgerroute \
    && useradd --uid 10001 --gid ledgerroute --no-create-home ledgerroute \
    && mkdir -p var \
    && chown ledgerroute:ledgerroute var

USER ledgerroute
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).close()"]
CMD ["python", "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
