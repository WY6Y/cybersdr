#!/usr/bin/env bash
# install.sh — CyberSDR one-shot setup
# Run as the wy6y user from /home/wy6y/cybersdr/
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== CyberSDR install ==="

# 1. Copy env file if absent
if [[ ! -f .env ]]; then
    cp .env.example .env
    echo "[1/5] Created .env from .env.example — EDIT before continuing"
    echo "      Set RTL_TCP_HOST, MY_CALL, MY_GRID, etc., then re-run this script."
    exit 0
fi
echo "[1/5] .env present"

# 2. Generate PWA icons
echo "[2/5] Generating PWA icons…"
if python3 -c "import PIL" 2>/dev/null; then
    python3 static/icons/generate_icons.py
else
    echo "      Pillow not installed — skipping icon generation (pip install Pillow to generate icons)"
    # Create placeholder 1×1 PNGs so the manifest doesn't 404
    python3 - <<'EOF'
import struct, zlib, os

def tiny_png(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Minimal 1x1 cyan PNG
    def chunk(name, data):
        c = struct.pack('>I', len(data)) + name + data
        return c + struct.pack('>I', zlib.crc32(name + data) & 0xFFFFFFFF)
    raw = b'\x00\x00\xf5\xff\xff'  # filter + R G B A (cyan opaque)
    idat = zlib.compress(raw)
    png = (b'\x89PNG\r\n\x1a\n'
           + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
           + chunk(b'IDAT', idat)
           + chunk(b'IEND', b''))
    with open(path, 'wb') as f:
        f.write(png)
    print(f"  Placeholder written: {path}")

tiny_png("static/icons/icon-192.png")
tiny_png("static/icons/icon-512.png")
EOF
fi

# 3. Build and start Docker container
echo "[3/5] Building Docker image…"
docker compose build

echo "      Starting container…"
docker compose up -d
echo "      Container started — CyberSDR is at http://localhost:5020"

# 4. Install systemd service
echo "[4/5] Installing cybersdr.service…"
SERVICE_FILE=/etc/systemd/system/cybersdr.service

sudo tee "$SERVICE_FILE" > /dev/null <<UNIT
[Unit]
Description=CyberSDR WSPR Dashboard
Documentation=https://github.com/wy6y/cybersdr
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${SCRIPT_DIR}
ExecStart=/usr/bin/docker compose up -d --remove-orphans
ExecStop=/usr/bin/docker compose down
ExecReload=/usr/bin/docker compose restart
User=wy6y
TimeoutStartSec=120

[Install]
WantedBy=multi-user.target
UNIT

# 5. Enable and start the service
echo "[5/5] Enabling and starting cybersdr.service…"
sudo systemctl daemon-reload
sudo systemctl enable cybersdr.service
sudo systemctl start cybersdr.service

echo ""
echo "=== Install complete ==="
echo "  Service:   sudo systemctl status cybersdr"
echo "  Logs:      docker compose logs -f"
echo "  Dashboard: http://$(hostname -I | awk '{print $1}'):5020"
echo ""
echo "  Add to Caddy (/etc/caddy/Caddyfile):"
echo "    sdr.wy6y.net {"
echo "      tls internal"
echo "      reverse_proxy 127.0.0.1:5020"
echo "    }"
echo "  Then: sudo systemctl reload caddy"
