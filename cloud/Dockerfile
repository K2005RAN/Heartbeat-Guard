# MayaCardio Cloud Service Dockerfile
# Lightweight, single-stage, runs anywhere (Fly.io, Render, AWS Free Tier, Docker)
FROM python:3.11-slim

WORKDIR /app

# Copy application files
COPY pipeline /app/pipeline
COPY cloud /app/cloud

ENV PYTHONUNBUFFERED=1
ENV MAYACARDIO_CLOUD_PORT=8000
ENV MAYACARDIO_AUTH_TOKEN="mayacardio_secret_token_2026"

EXPOSE 8000

CMD ["python3", "-m", "cloud.service", "--port", "8000", "--db-path", "/data/cloud_data.db"]
