FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    EGASIS_SIMULATION=true \
    EGASIS_DATABASE_URL=sqlite:////app/data/egasis.db

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 egasis \
    && useradd --uid 10001 --gid egasis --no-create-home --shell /usr/sbin/nologin egasis \
    && mkdir -p /app/data \
    && chown egasis:egasis /app/data

COPY --chown=egasis:egasis egasis ./egasis
COPY --chown=egasis:egasis static ./static
USER egasis
VOLUME ["/app/data"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3)" || exit 1
CMD ["python", "-m", "uvicorn", "egasis.app:app", "--host", "0.0.0.0", "--port", "8765"]
