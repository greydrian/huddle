# Python 3.14 (current stable series as of 2026, full arm64 wheel coverage
# confirmed for uvicorn's C-extension deps) — runs the same on your dev
# machine and on the Raspberry Pi 5 (aarch64), since Docker abstracts that
# away entirely.
FROM python:3.14-slim

WORKDIR /app

# Keep logs unbuffered (so `docker logs` shows them immediately) and skip
# writing .pyc files into the image layer.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data

# Install dependencies first so this layer is cached across code-only rebuilds.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Non-root user, per container security best practice. /data is created and
# owned here so a fresh named volume mounted at this path inherits the right
# permissions on first run (see docker-compose.yml).
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 8000

# No external curl dependency — just hit our own /health route with the
# stdlib, keeping the image slim.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)"]

# No --reload here — that's dev-only. Use docker-compose.override.yml for that.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
