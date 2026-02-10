# Node Infra

Infrastructure scripts for Retina Node fleet management.

## Mender Auto-Accept

Automatically accepts pending Mender devices. Runs on your central server.

### Quick Deploy

```bash
# Clone to your server
git clone https://github.com/offworldlabs/node-infra.git /opt/node-infra
cd /opt/node-infra/mender-auto-accept

# Configure (add your Mender PAT)
cp .env.example .env
nano .env  # Add MENDER_PAT=your-token

# Install systemd timer
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mender-auto-accept.timer

# Verify
systemctl status mender-auto-accept.timer
journalctl -u mender-auto-accept -f
```

### Test locally

```bash
cd mender-auto-accept
export MENDER_PAT=your-token
python3 auto_accept.py
```

### Configuration

Edit `.env`:

| Variable | Required | Description |
|----------|----------|-------------|
| `MENDER_PAT` | Yes | Personal Access Token from Mender UI |
| `MENDER_SERVER` | No | Default: `https://hosted.mender.io` |
| `DEVICE_TYPE_FILTER` | No | Only accept this device type |
