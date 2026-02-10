# Node Infra

Infrastructure scripts for Retina Node fleet management.

## Mender Auto-Accept

Automatically accepts pending Mender devices. Runs on your central server (not on devices).

### Install

```bash
# 1. Clone to server
git clone https://github.com/offworldlabs/node-infra.git /opt/node-infra
cd /opt/node-infra/mender-auto-accept

# 2. Install Python dependencies
pip3 install -r requirements.txt

# 3. Configure
cp .env.example .env
nano .env  # Add your MENDER_PAT

# 4. Install systemd timer (runs every 2 min)
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mender-auto-accept.timer

# 5. Verify
systemctl status mender-auto-accept.timer
journalctl -u mender-auto-accept -f
```

### Test locally

```bash
cd /opt/node-infra/mender-auto-accept
pip3 install -r requirements.txt
export MENDER_PAT=your-token
export NODE_ID_PREFIX=ret
python3 auto_accept.py
```

### Configuration

Edit `.env`:

| Variable | Required | Description |
|----------|----------|-------------|
| `MENDER_PAT` | Yes | Personal Access Token from Mender UI |
| `MENDER_SERVER` | No | Default: `https://hosted.mender.io` |
| `NODE_ID_PREFIX` | No | Only accept nodes with this prefix (e.g., `ret`) |

### How it works

1. Timer triggers every 2 minutes
2. Script fetches pending devices from Mender API
3. Filters by `node_id` prefix (if configured)
4. Accepts matching devices
