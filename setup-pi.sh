#!/bin/bash
# ============================================================
# Web Vision Agent — Setup Script for Raspberry Pi
# ============================================================
#
# INSTRUCTIONS FOR LLM ASSISTANT:
# This script installs Docker and builds the Web Vision Agent
# container on a Raspberry Pi 5 (ARM64, 8GB RAM).
#
# PREREQUISITES:
# - Raspberry Pi 5 with 8GB RAM
# - Raspberry Pi OS (64-bit) or Ubuntu Server 24.04 ARM64
# - Internet connection
# - At least 4GB free disk space
#
# HOW TO RUN:
#   chmod +x setup-pi.sh
#   ./setup-pi.sh
#
# WHAT THIS SCRIPT DOES:
# 1. Installs Docker if not present
# 2. Adds current user to docker group
# 3. Builds the Docker image (takes 5-10 minutes on Pi)
# 4. Starts the container
# 5. Runs a health check
#
# TROUBLESHOOTING TIPS FOR LLM:
# - If "docker: command not found" → Docker didn't install, check internet
# - If build fails on "chromium" → try "chromium-browser" package instead
# - If container exits immediately → check logs: docker logs web-vision-agent
# - If "permission denied" on docker → run: sudo usermod -aG docker $USER && newgrp docker
# - If port 8080 is busy → change API_PORT in docker-compose.yml
# - If "shm" errors → the --shm-size flag is critical for Chromium
# - If Chromium crashes inside container → check RAM: free -h (need 2GB+ free)
# - If LLM servers unreachable → check: curl http://192.168.31.195:1234/v1/models
#
# AFTER SUCCESSFUL INSTALL:
# - API is available at: http://<pi-ip>:8080
# - VNC is available at: <pi-ip>:5900 (no password)
# - Test with: curl http://localhost:8080/health
# - Open a site: curl -N http://localhost:8080/open -H "Content-Type: application/json" -d '{"url":"https://example.com"}'
#
# NETWORK REQUIREMENTS:
# - LLM servers available at http://192.168.31.195:1234 (Gemma + UI-TARS)
# - Ensure the Pi can reach this host on port 1234
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "============================================"
echo " Web Vision Agent — Raspberry Pi Setup"
echo "============================================"
echo ""

# --- Step 1: Check architecture ---
ARCH=$(uname -m)
if [[ "$ARCH" != "aarch64" && "$ARCH" != "arm64" ]]; then
    echo -e "${YELLOW}WARNING: This script is designed for ARM64 (Raspberry Pi).${NC}"
    echo "Detected architecture: $ARCH"
    echo "Continuing anyway..."
    echo ""
fi

# --- Step 2: Install Docker ---
if command -v docker &> /dev/null; then
    echo -e "${GREEN}✓ Docker is already installed${NC}"
    docker --version
else
    echo "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER"
    echo -e "${GREEN}✓ Docker installed${NC}"
    echo ""
    echo -e "${YELLOW}NOTE: You may need to log out and back in for docker group to take effect.${NC}"
    echo "If docker commands fail with 'permission denied', run:"
    echo "  newgrp docker"
    echo ""
fi

# --- Step 3: Ensure docker is running ---
if ! docker info &> /dev/null 2>&1; then
    echo "Starting Docker daemon..."
    sudo systemctl start docker
    sudo systemctl enable docker
    sleep 3
fi

# --- Step 4: Check Docker Compose ---
if docker compose version &> /dev/null 2>&1; then
    echo -e "${GREEN}✓ Docker Compose is available${NC}"
else
    echo -e "${RED}ERROR: Docker Compose not found.${NC}"
    echo "Try: sudo apt-get install docker-compose-plugin"
    exit 1
fi

# --- Step 5: Check network connectivity to LLM servers ---
echo ""
echo "Checking LLM server connectivity..."

if curl -s --max-time 5 http://192.168.31.195:1234/v1/models > /dev/null 2>&1; then
    echo -e "${GREEN}✓ LLM server (192.168.31.195:1234) is reachable${NC}"
else
    echo -e "${YELLOW}⚠ LLM server (192.168.31.195:1234) is NOT reachable${NC}"
    echo "  The agent will not work without this server."
    echo "  Make sure LM Studio is running on that machine."
fi

# --- Step 6: Build Docker image ---
echo ""
echo "Building Docker image (this may take 5-10 minutes on Pi)..."
echo ""

cd "$SCRIPT_DIR"
docker compose build

echo ""
echo -e "${GREEN}✓ Docker image built successfully${NC}"

# --- Step 7: Start container ---
echo ""
echo "Starting Web Vision Agent container..."
docker compose up -d

echo ""
echo "Waiting 10 seconds for services to initialize..."
sleep 10

# --- Step 8: Health check ---
echo ""
echo "Running health check..."

HEALTH=$(curl -s --max-time 10 http://localhost:8080/health 2>/dev/null || echo "FAILED")

if echo "$HEALTH" | grep -q '"status"'; then
    echo -e "${GREEN}✓ Web Vision Agent is running!${NC}"
    echo "  Health response: $HEALTH"
else
    echo -e "${RED}✗ Health check failed${NC}"
    echo "  Checking container logs..."
    echo ""
    docker logs web-vision-agent --tail 30
    echo ""
    echo "TROUBLESHOOTING:"
    echo "  1. Check logs: docker logs web-vision-agent"
    echo "  2. Check if container is running: docker ps"
    echo "  3. Try restarting: docker compose restart"
    exit 1
fi

# --- Done ---
echo ""
echo "============================================"
echo -e "${GREEN} SETUP COMPLETE!${NC}"
echo "============================================"
echo ""
echo "API endpoint:  http://$(hostname -I | awk '{print $1}'):8080"
echo "VNC viewer:    $(hostname -I | awk '{print $1}'):5900"
echo ""
echo "Quick test commands:"
echo "  curl http://localhost:8080/health"
echo "  curl -N http://localhost:8080/open -H 'Content-Type: application/json' -d '{\"url\":\"https://example.com\"}'"
echo ""
echo "To view logs:    docker logs -f web-vision-agent"
echo "To stop:         docker compose down"
echo "To restart:      docker compose restart"
echo ""
