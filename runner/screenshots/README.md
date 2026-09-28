# The guide's screenshots

The pictures in `docs/images/` are the real dashboard, taken by a script of a
demo rig. Take them again after a change to `rig/web/`, and commit them with
it, so the guide shows the dashboard as it is.

| File | What it does |
|---|---|
| `demo_rig.py` | Writes a demo rig's state: four boards (made-up names, locally administered MACs), their health check verdicts and chip readings, the host's health, the installed version, and a week of runs with their reports, results and bundles. |
| `demo_after_start.py` | Run once the service is up. A start runs a discovery, which finds no boards on a host without any, and marks any running run interrupted: this puts the demo's board snapshot back and starts one run that stays in progress. |
| `capture.py` | Opens each page in Chromium (Playwright), signed in with the demo's key, and writes one WebP per page. |

Nothing here touches hardware or a real rig's state. Point `capture.py` only
at the demo.

## The recipe

In a Linux container or a throwaway virtualenv, from the repository root:

```bash
python -m pip install -e ./core -e ./rig
export STATE=/tmp/demo-rig
python runner/screenshots/demo_rig.py
cp $STATE/inventory.json $STATE/inventory.demo.json
openssl rand -hex 24 > /tmp/demo-key
ALTERIOM_HIL_STATUS_FILE=$STATE/status.json ALTERIOM_HIL_VERSION_FILE=$STATE/version.json \
  alteriom-hil-service --mode standalone --repo . --state $STATE \
  --registry $STATE/inventory.yaml --token-file /tmp/demo-key \
  --web-root rig/web --bind 127.0.0.1 --port 8090 &
```

Once the service answers `/healthz`, restore the demo and name the rig's place:

```bash
PYTHONPATH=runner/screenshots python runner/screenshots/demo_after_start.py
curl -s -X POST -H "Authorization: Bearer $(cat /tmp/demo-key)" -H "Content-Type: application/json" \
  -d '{"description": "Four boards on a powered hub", "location": "Bench by the window"}' \
  http://127.0.0.1:8090/api/v1/rig/details
```

Then take the pictures:

```bash
python -m pip install playwright pillow && python -m playwright install --with-deps chromium
BASE=http://127.0.0.1:8090 KEY=$(cat /tmp/demo-key) OUT=docs/images python runner/screenshots/capture.py
```

The rig's name in the pictures is the host's name; run it in a container with
`--hostname bench-01` to match the ones committed. `DEMO_VERSION` sets the
version the demo says it runs.

## Notes

- **The page allows no eval.** Its content security policy blocks the script
  Playwright's waits evaluate, so the capture opens pages with CSP bypassed.
  The page served is the same page a person gets.
- **The console bar is hidden** in the pictures: it is fixed to the window and
  in a still it only covers the page.
- **Times are relative to now.** The demo's runs are written hours and days
  before the moment it runs, so "3h ago" stays true whenever it is taken.
