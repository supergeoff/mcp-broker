FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LISTEN_HOST=0.0.0.0 \
    LISTEN_PORT=8080 \
    DATABASE_URL=sqlite+aiosqlite:////data/mcp-broker.sqlite3

WORKDIR /app

RUN addgroup --system broker \
    && adduser --system --ingroup broker broker \
    && mkdir -p /data \
    && chown -R broker:broker /app /data

COPY pyproject.toml uv.lock README.md ./
COPY mcp_broker ./mcp_broker

RUN pip install --no-cache-dir .

USER broker

EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).read()"

CMD ["sh", "-c", "uvicorn mcp_broker.main:app --host ${LISTEN_HOST} --port ${LISTEN_PORT}"]
