#!/bin/bash
set -e

REPO="https://github.com/sunjoograci/cityarts-teacher-search-tool.git"
APP_DIR="/opt/cityarts"
DATA_DIR="/data"
PORT=8080

echo "==> Updating system packages..."
sudo apt-get update -y && sudo apt-get upgrade -y

echo "==> Opening port $PORT in OS firewall..."
sudo iptables -I INPUT -p tcp --dport $PORT -j ACCEPT
sudo sh -c "iptables-save > /etc/iptables/rules.v4" 2>/dev/null || true
# Persistent across reboots on Ubuntu
sudo apt-get install -y iptables-persistent

echo "==> Installing Docker..."
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"

echo "==> Creating persistent data directory..."
sudo mkdir -p "$DATA_DIR"
sudo chown "$USER":"$USER" "$DATA_DIR"

echo "==> Cloning repository..."
sudo rm -rf "$APP_DIR"
sudo git clone "$REPO" "$APP_DIR"
sudo chown -R "$USER":"$USER" "$APP_DIR"

echo "==> Building Docker image (this takes a few minutes)..."
cd "$APP_DIR"
sudo docker build -t cityarts .

echo "==> Starting app..."
sudo docker run -d \
  --name cityarts \
  --restart always \
  -v "$DATA_DIR":/data \
  -e DB_PATH=/data/schools.db \
  -p $PORT:$PORT \
  -e PORT=$PORT \
  cityarts

echo ""
echo "✓ Done! App is running at http://$(curl -s ifconfig.me):$PORT"
