FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MORPHLAKE_MODELS_CONFIG=/app/config/models.yaml

WORKDIR /app

RUN groupadd --system morphlake \
    && useradd --system --gid morphlake --home-dir /app morphlake \
    && mkdir -p /app/data \
    && chown morphlake:morphlake /app/data

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config

RUN pip install --upgrade pip \
    && pip install .

USER morphlake
EXPOSE 8080 8081

CMD ["uvicorn", "morphlake.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
