# Python 3.14 (current stable series as of 2026) — targets the GMKtec G10
# deployment server (x86_64 Ubuntu/Debian), same as your dev machine, so
# there's no cross-architecture wheel concern here.
# Pinned by digest so a rebuild gets the exact same base; the tag is kept for
# readability. Dependabot (docker ecosystem, monthly) bumps the digest.
FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d

WORKDIR /app

# Keep logs unbuffered (so `docker logs` shows them immediately) and skip
# writing .pyc files into the image layer.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data

# Install dependencies first so this layer is cached across code-only rebuilds.
# requirements.txt is generated from requirements.in with every package pinned
# and hashed, so --require-hashes refuses anything that doesn't match.
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY app/ ./app/

# Non-root user, per container security best practice. /data is created and
# owned here so the bind-mounted host directory inherits the right
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
