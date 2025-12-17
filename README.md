# blah2 Node Infrastructure

Infrastructure for building Mender-enabled OS images and update artifacts for blah2 radar deployment. Pre-loaded with system requirements (Docker, SDRplay API, Graphviz) and designed for automated CI deployment to Raspberry Pi 5.

## Quick Start

### Initial Installation

Before OTA updates can be applied, a base OS image must be flashed to an SD card.

1. **Download the latest release** from [Releases](https://github.com/offworldlabs/node-infra/releases)

2. **Flash to SD card** using Raspberry Pi Imager:
   - Device: Raspberry Pi 5
   - OS: Custom → Select `blah2-os-vx.x.x.img`
   - Storage: Your SD card
   - **Important:** Do not apply any OS customisation settings

3. **Boot the Pi** and wait 30-60 seconds for startup

4. **Configure WiFi** via captive portal:
   - Connect to WiFi network `node-setup`
   - Portal should open automatically (or navigate to http://192.168.42.1)
   - Enter your WiFi SSID and password
   - Node will reboot and connect to your network

5. **Accept device** in Mender dashboard:
   - Node will appear as "pending" after connecting to WiFi
   - Accept the device to enable OTA updates

6. **Deploy blah2-stack** OTA via Mender

### Creating a Release

Tag a commit with `os-vx.x.x` and push:
```bash
git tag os-v1.0.0
git push origin os-v1.0.0
```

This triggers the GHA workflow (`.github/workflows/build_os.yml`) which:
1. Builds OS image and Mender artifact
2. Uploads to GitHub Releases
3. Uploads Mender artifact to OffWorld Lab Mender server

> **Note:** Currently triggers on any `os-v*` tag. TODO: Change to only PR merges into main.

## Configuration

### SSH Access

public SSH keys can be added to the `ssh_pub_keys/` folder:
```bash
cp ~/.ssh/id_ed25519.pub ssh_pub_keys/yourname.pub
```

Keys in this folder are automatically added to deployed nodes.

### Mender Tenant Token

**For GitHub Actions:** tenant token is added via GH secrets.

**For local builds:** Create a custom config file:
```bash
echo 'mender_tenant_token: "YOUR_TOKEN_HERE"' > configuration/mender/mender_custom.yml
```

This file is `.gitignore`d to prevent accidental commits.

## Building from Source

### Requirements

Install [EDI](https://docs.get-edi.io/en/stable/getting_started_v2.html) and dependencies:

**Ubuntu 24.04 or newer:**
```bash
sudo apt install buildah containers-storage crun curl distrobox \
  dosfstools e2fsprogs fakeroot genimage git mender-artifact \
  mmdebstrap mtools parted python3-sphinx python3-testinfra \
  podman rsync zerofree
```

### Build
```bash
edi -v project make owl-os-pi5.yml
```

**Output artifacts:**
- `owl-os-vx.x.x.img` - Flashable OS image with A/B partitioning
- `owl-os-vx.x.x.mender` - OTA update artifact

### Clean
```bash
edi -v project clean owl-os-pi5.yml
```

## Default Credentials

- **Username:** `pi`
- **Password:** `raspberry`

## Credits

Built using [EDI-PI](https://github.com/lueschem/edi-pi) by [lueschem](https://github.com/lueschem).