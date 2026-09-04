# Node Infra

Infrastructure scripts for Retina Node fleet management.

## Mender Auto-Accept

Automatically accepts pending Mender devices. Runs on your central server (not on devices).

### Install

```bash
# 1. Clone to server
git clone https://github.com/offworldlabs/node-infra.git ~/retina/node-infra
cd ~/retina/node-infra/mender-auto-accept

# 2. Install Python dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
nano .env  # Add your MENDER_PAT

# 4. Install systemd timer (runs every 30s)
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mender-auto-accept.timer

# 5. Verify
systemctl status mender-auto-accept.timer
journalctl -u mender-auto-accept -f
```

### Test locally

```bash
cd ~/retina/node-infra/mender-auto-accept
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export MENDER_PAT=your-token
export NODE_ID_PREFIX=ret
python auto_accept.py
```

### Configuration

Edit `.env`:

| Variable | Required | Description |
|----------|----------|-------------|
| `MENDER_PAT` | Yes | Personal Access Token from Mender UI |
| `MENDER_SERVER` | No | Default: `https://hosted.mender.io` |
| `NODE_ID_PREFIX` | No | Only accept nodes with this prefix (e.g., `ret`) |

### How it works

1. Timer triggers every 30 seconds
2. Script fetches pending devices from Mender API
3. Filters by `node_id` prefix (if configured)
4. Accepts matching devices


## Failed-deployment retry

Re-issues Mender deployments that failed **before the artifact reached the
node**, so an update lost to a broken download recovers without anyone
deploying it by hand.

Hosted Mender has a "Retries" field on a deployment, but it is a paid-plan
feature: on the `os` plan, creating a deployment with one is rejected with
`403 Feature not available in your Plan.` The client's own retry does not cover
this either. It resumes a broken download, but once the CDN answers with an
unexpected HTTP status it treats that as fatal and fails the deployment with
most of its ten retries unused.

### What it will and will not retry

Only one failure is considered fixable: the artifact never finished arriving.
Nothing was written to the node and nothing about the node caused it, so an
identical request has an independent chance of succeeding.

Everything else is refused, **including failures it cannot read**. The costs are
asymmetric: a missed retry costs one hand deployment, while a wrong retry
reboots a live node on a timer for a reason that will not change. So the test is
an allowlist and the default is to do nothing.

| In the final attempt of the device log | Retried |
|---|---|
| `Unexpected status code while fetching artifact` | yes |
| `Giving up on resuming the download` | yes |
| `No space left on device`, too big, incompatible, bad signature | no |
| `Process returned non-zero exit status`, `ArtifactRollback` | no |
| Anything else, an unreadable log, no log at all | no |

Two properties of Mender's device log make the naive version of this wrong, and
both were measured against real fleet logs:

* **The log is cumulative across attempts.** One node's carries three attempts
  at the same deployment, plus lines dated four months earlier from a clock
  skew. Only the text after the final `Deployment with ID ... started` is
  classified, so an old disk-full line cannot veto a retry forever, and an old
  transport error cannot authorise one.
* **`Installing artifact...` does not mark the install phase.** It is printed
  about a second after the deployment starts, before the download. On one node
  it appears five hours before the download gives up. Phase cannot be inferred
  from it.

### Other limits

* A retry is skipped while the device is already running a deployment, if the
  artifact has landed since the failure, or if the device is no longer accepted.
* `DEPLOY_RETRY_MAX_ATTEMPTS` (2) per device+artifact, then it stops and leaves
  it for a person.
* `DEPLOY_RETRY_MAX_PER_PASS` (5) caps one pass, so a fleet-wide outage cannot
  become a fleet-wide burst.
* Attempt counts are written before the next deployment is created, and the pass
  aborts up front if they cannot be written at all. Deployments that cannot be
  counted are how a retry becomes a storm.

### Install

```bash
sudo cp systemd/retina-deploy-retry.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now retina-deploy-retry.timer
```

**The unit runs without `--apply`, so it creates nothing.** It reports a verdict
on every real failure and leaves them alone. That is deliberate: the create path
has never run against the fleet, and the classifier reads log wording that only
Mender controls, so the journal earns the trust first.

```bash
journalctl -u retina-deploy-retry
```

If its verdicts match the calls you would have made, add `--apply` to
`ExecStart` and `systemctl daemon-reload`. Until then a failure it would have
retried still needs a deployment by hand.

### Check what it would do, by hand

```bash
cd ~/retina/node-infra/mender-auto-accept
MENDER_PAT=your-token .venv/bin/python deploy_retry.py
```
