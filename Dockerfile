FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MORPHLAKE_MODELS_CONFIG=/app/config/models.yaml

WORKDIR /app

RUN groupadd --system morphlake \
    && useradd --system --gid morphlake --home-dir /app morphlake

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY config ./config

RUN pip install --upgrade pip \
    && pip install .

USER morphlake
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=3)"

CMD ["uvicorn", "morphlake.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
