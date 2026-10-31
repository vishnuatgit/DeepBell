# DeepBell: one FastAPI process. It runs a single uvicorn worker on purpose: the watchdog,
# the snapshot thread, open incidents and Chaos Lab state all live in that one process.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEEPBELL_DATA_DIR=/app/var/data \
    DEEPBELL_BACKUP_DIR=/app/var/backups \
    DEEPBELL_REPORTS_DIR=/app/var/reports

WORKDIR /app

COPY target_app/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

# Run as an unprivileged user; only /app/var (databases, snapshots, reports) is writable.
RUN useradd --create-home --uid 10001 deepbell \
    && mkdir -p /app/var \
    && chown deepbell:deepbell /app/var

COPY target_app ./target_app

USER deepbell
EXPOSE 8000

# Shell form so $PORT (set by hosts such as Render) is expanded; exec makes uvicorn PID 1.
CMD ["sh", "-c", "exec uvicorn main:app --app-dir target_app --host 0.0.0.0 --port ${PORT:-8000}"]
