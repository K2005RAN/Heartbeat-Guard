#!/usr/bin/env bash
# cloud/deploy.sh - One-command deployment script for MayaCardio Cloud Service
# Supports:
#   1. Local Docker container
#   2. Fly.io free tier
#   3. Render free tier / VM

set -euo pipefail

TARGET="${1:-local}"

echo "======================================================================"
echo "DEPLOYING MAYACARDIO CLOUD SERVICE (Target: $TARGET)"
echo "======================================================================"

case "$TARGET" in
  local)
    echo "Building and running local Docker container on port 8000..."
    docker build -t mayacardio-cloud -f cloud/Dockerfile .
    docker run -d --name mayacardio-cloud -p 8000:8000 -v mayacardio_data:/data mayacardio-cloud
    echo "Cloud service is running locally at http://127.0.0.1:8000"
    ;;
  fly)
    echo "Deploying to Fly.io free tier..."
    if ! command -v flyctl &> /dev/null; then
        echo "ERROR: flyctl CLI not installed. Install via: curl -L https://fly.io/install.sh | sh"
        exit 1
    fi
    flyctl launch --no-deploy --copy-config --name mayacardio-cloud-service
    flyctl deploy
    ;;
  *)
    echo "Unknown target: $TARGET. Supported: local, fly"
    exit 1
    ;;
esac
