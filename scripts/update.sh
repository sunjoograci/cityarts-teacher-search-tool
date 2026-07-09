#!/bin/bash
set -e

APP_DIR="/opt/cityarts"
PORT=8080

echo "==> Pulling latest code..."
cd "$APP_DIR"
sudo git pull

echo "==> Rebuilding Docker image..."
sudo docker build -t cityarts .

echo "==> Restarting app..."
sudo docker stop cityarts 2>/dev/null || true
sudo docker rm cityarts 2>/dev/null || true
sudo docker run -d \
  --name cityarts \
  --restart always \
  -v /data:/data \
  -e DB_PATH=/data/schools.db \
  -p $PORT:$PORT \
  -e PORT=$PORT \
  cityarts

echo ""
echo "✓ Updated! App is running at http://$(curl -s ifconfig.me):$PORT"
