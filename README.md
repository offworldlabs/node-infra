# Owl OS

Mender-enabled OS images for Raspberry Pi 5 radar nodes. The image comes pre-loaded with Docker, SDRplay API, Avahi mDNS, and a WiFi captive portal for easy setup.

##  Contents

- **Docker CE** with Compose plugin
- **SDRplay API v3.15** for RSPDuo hardware
- **Avahi mDNS** for `<hostname>.local` discovery
- **WiFi Connect** captive portal for network setup
- **Mender client** for OTA updates

## Quick Start

1. **Flash the image** to an SD card using Raspberry Pi Imager
   - Download from [Releases](https://github.com/offworldlabs/node-infra/releases)
   - Select `owl-os-vx.x.x.img` as custom OS
   - Do not apply OS customisation settings

2. **Boot and connect to WiFi**
   - Connect to the `node-setup` WiFi network
   - Captive portal opens automatically (or go to http://192.168.42.1)
   - Enter your WiFi credentials - node reboots and connects

3. **Accept device in Mender**
   - Node appears as "pending" once online
   - Accept to enable OTA updates

4. **Deploy [retina-node](https://github.com/offworldlabs/retina-node) stack** via Mender OTA

## Creating a Release

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

### SSH Access (Dev Only)

Public SSH keys can be added to `ssh_pub_keys/`:
```bash
cp ~/.ssh/id_ed25519.pub ssh_pub_keys/yourname.pub
```

Keys are baked into the image at build time.

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

- **Username:** `node`
- **Password:** `raspberry`

## Credits

Built using [EDI-PI](https://github.com/lueschem/edi-pi) by [lueschem](https://github.com/lueschem).