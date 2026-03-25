# COP Engine — Production Dockerfile
FROM python:3.11-slim AS builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-slim
RUN groupadd -r cop && useradd -r -g cop -m cop
WORKDIR /app

# Runtime deps for reportlab/weasyprint
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local
COPY *.py .
COPY adapters/ ./adapters/

RUN mkdir -p /app/data /app/logs && chown -R cop:cop /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    COP_ENV=production \
    COP_DEMO=true \
    PORT=8000

USER cop
EXPOSE ${PORT}

CMD uvicorn api:app --host 0.0.0.0 --port $PORT --workers 2
