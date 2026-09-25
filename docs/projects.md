# Projects and your CI

How the rig gets *your* firmware and runs *your* suite. Two things live in
your repository: a CI job that builds a **bundle**, and a **pytest suite**
that the rig runs against the boards. The rig adds the project by its URL,
fetches the bundle its CI built, checks the suite out for each run, flashes,
runs, and keeps the evidence.

The smallest working project is
[Alteriom/esp32-rig-example](https://github.com/Alteriom/esp32-rig-example):
a firmware that answers the rig, a suite of three tests, and the workflow
that builds the bundle. Fork it, or copy the three pieces into your project.

## 1. GitHub first

The rig needs a token to read your repository and its Actions artifacts. A
fine-grained personal access token with **Contents: read** and **Actions:
read** on the repositories you will test. Give it to the rig under
**Settings → Rig → GitHub**, or on the host:

```bash
sudo alteriom-hil-admin github set      # reads the token from the terminal, never from argv
sudo alteriom-hil-admin github check    # who the token is, and that GitHub accepts it
```

The token is kept on the rig (`/var/lib/alteriom-hil/github-token`, readable
by the rig's user only) and is never shown back. What the rig does show, on
that card, is everything else about it: who GitHub says it is, what kind of
token it is (fine-grained or classic), **when it expires** (with a warning on
the Overview two weeks ahead), where it came from (this page, or the host's
file), **which private repositories it was given** (GitHub shows every
public repository to any token, so those are counted apart), and, per
project, whether it can see the repository, read its code, and list its
Actions artifacts. **Check
again** asks GitHub now, after you changed the token there. Without a token,
**Add project** is not offered: a rig cannot add a project it cannot read.

!!! tip "A fine-grained token reaches only the repositories you gave it"
    When adding a project is refused, the refusal names the private
    repositories the token was given and where on GitHub to add the new one
    (the token's *Repository access*). Widening the token is done on GitHub; the rig then
    sees it on the next check.

## 2. Add the project

**Settings → Projects → Add project.** Paste the repository's URL, press
**Look it up**. The rig asks GitHub about the repository with its token and
fills the form in: the default branch, where the pytest suite seems to live,
which workflow looks like the one that builds firmware, and which board
families the workflow builds for. It says which of those it found and which
it guessed; check them and press **Add**. The fields:

| Field | Meaning | Default |
|---|---|---|
| **Repository** | the GitHub repository the rig checks out for every run | — |
| **Default branch** | what a run is for when it names no ref | the repository's own |
| **Suite path** | the directory in the repository the rig runs pytest in | `tests` |
| **Families** | the chip families a run takes one board of each; none means the whole bench -- the boards of the families the run chose, which are the ones its bundle carries | none |
| **Supply workflow** | the workflow whose artifact is the bundle | `.github/workflows/hil.yml` |
| **Artifact name** | the Actions artifact that workflow uploads | `hil-artifacts` |
| **Revision key** | the key in the bundle's manifest that holds the commit | `git_sha`, or what the repository's `.alteriom-hil.yaml` declares |
| **Timeout** | how long a run's suite may take | 1800 s |

The rig writes the project as a profile document under
`/var/lib/alteriom-hil/profiles/<name>.yaml`, reads it back at once, and an
upgrade leaves it alone. Edit or remove a project from its page; a project
that shipped with the rig (the Rig example, and painlessMesh, the reference) can be removed and
restored. The same over the API: `GET`/`POST /api/v1/projects`,
`POST /api/v1/projects/inspect`, `POST /api/v1/projects/<name>` to edit,
`.../delete`, `.../restore`, with an admin key.

## 3. The bundle your CI builds

**The rig does not build firmware.** Your CI does, the way it already does,
and the rig flashes exactly that, so it can say afterwards precisely what it
flashed. A bundle is a directory:

```
hil-artifacts/
  manifest.json
  esp32/flash-image.bin        (+ bootloader.bin, partitions.bin, boot_app0.bin, firmware.bin)
  esp32-c3/flash-image.bin     …
```

`manifest.json` is schema 2:

```json
{
  "schema": 2,
  "git_sha": "<the 40-character commit the build resolved to>",
  "targets": {
    "esp32": {
      "chip": "esp32",
      "board": "esp32dev",
      "image": "esp32/flash-image.bin",
      "flash_offset": "0x0",
      "sha256": "<digest of the image>",
      "segments": {"bootloader.bin": "0x1000", "partitions.bin": "0x8000", "boot_app0.bin": "0xe000", "firmware.bin": "0x10000"},
      "files": {"bootloader.bin": {"sha256": "…"}, "partitions.bin": {"sha256": "…"}, "boot_app0.bin": {"sha256": "…"}, "firmware.bin": {"sha256": "…"}}
    }
  }
}
```

- The **revision key** (`git_sha` unless you set another) holds the commit.
  That is what a run is named by, what a bundle is reused by, and what the
  dashboard links. A bundle whose manifest holds the commit under a key the
  project does not name is refused, and the refusal says which key does.
- **`image`** is one merged image per family, flashed whole at
  `flash_offset`. `segments` and `files` are optional; when present the rig
  checks each component sits at its stated offset inside the merged image.
- The rig re-hashes every image and every component before it flashes. A
  flash is the one step that cannot be undone by retrying, and a bundle
  corrupted in transit would otherwise be diagnosed as a firmware bug on
  hardware.

The example's `scripts/build_bundle.py` builds this with PlatformIO and
`esptool merge-bin`: one `pio run -e <family>` per family, the merged image
from the bootloader, partition table, OTA selector and application at their
offsets (the ESP8266 is one image at `0x0`), and the manifest. It is about a
hundred lines and it is yours to change.

## 4. The workflow

```yaml
# .github/workflows/hil.yml
name: HIL
on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:
jobs:
  bundle:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: {fetch-depth: 0}
      - uses: actions/setup-python@v5
        with: {python-version: "3.12"}
      - run: python -m pip install platformio esptool
      - run: python scripts/build_bundle.py --out hil-artifacts
      - uses: actions/upload-artifact@v4
        with:
          name: hil-artifacts        # the project's artifact name on the rig
          path: hil-artifacts        # the bundle directory's contents
          if-no-files-found: error
```

That is all a rig on your LAN needs: **Get firmware from GitHub** on the
project's page takes the newest `hil-artifacts` uploaded by a run of this
workflow, unpacks it, checks it, and holds it for runs. A run of any other
workflow is not taken, on purpose.

A CI that can reach the rig may hand the bundle over instead, in the same
job, with a key of role `user` or `admin` kept as a secret:

```yaml
      - name: Hand the bundle to the rig
        if: github.event_name != 'pull_request'
        env:
          RIG_URL: ${{ secrets.RIG_URL }}
          RIG_KEY: ${{ secrets.RIG_KEY }}
        run: |
          tar -czf bundle.tar.gz hil-artifacts
          curl -fsS -X POST -H "Authorization: Bearer $RIG_KEY" \
            --data-binary @bundle.tar.gz \
            "$RIG_URL/api/v1/artifacts?profile=esp32-rig-example&repo=https://github.com/$GITHUB_REPOSITORY&workflow=.github/workflows/hil.yml&run_id=$GITHUB_RUN_ID&run_url=$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID&commit=$GITHUB_SHA"
```

The rig checks the origin against the project's supply block, the commit
against the manifest, and the checksums; a bundle it already holds for that
commit is reused rather than stored twice.

## 5. The suite

Pytest, in the suite path, with the rig's fixtures available through the
`alteriom_hil` pytest plugin the rig installs. The one you want is `bank`: a
dictionary of board id to a **board client**, one per board the run was
given, each already checked responsive (a silent board is reset once and
named if it stays silent).

```python
# tests/test_it_answers.py
import pytest

pytestmark = pytest.mark.hil_only(reason="real silicon")


def test_every_board_boots_and_says_what_it_is(bank):
    for board_id, board in bank.items():
        info = board.ensure_responsive()
        assert info["family"], f"{board_id} did not say its family"
        assert int(info["freeHeap"]) > 0, f"{board_id} reports no free heap"


def test_the_firmware_adds(bank):
    for board_id, board in bank.items():
        reply = board.send_cmd_awaiting("sum", lambda e: e["evt"] == "sum", "a sum", timeout=5, a=20, b=22)
        assert reply["value"] == 42, board_id
```

The client talks newline-delimited JSON over the board's serial port. Your
firmware answers `{"cmd": "info"}` with `{"evt": "info", "family": …,
"bootId": …, "freeHeap": …}` and announces a start with `{"evt": "boot", …}`;
past that, the commands are yours. The client's methods:

| Method | Does |
|---|---|
| `info(timeout)` | sends `info`, returns the reply |
| `ensure_responsive()` | `info`, resetting the board once over RTS/DTR if it is silent |
| `send_cmd(cmd, **fields)` | writes one command frame |
| `wait_for(predicate, description, timeout)` | the first event matching, keeping the others for later waits |
| `send_cmd_awaiting(cmd, predicate, description, timeout, idempotent=False, **fields)` | send and wait, resending if the firmware said it could not parse the frame |
| `hard_reset()` | RTS/DTR reset, returns the `boot` event |
| `clear_pending()` | forget every event seen so far |

Markers the rig reads: `hil_only(reason)` for a test compile-only CI cannot
exercise (the example runs its suite in its own CI against the rig's
simulator, where those skip); `capability("name")` to say which capability a
test is evidence for, which the report groups by; `failure_class("…")` to
classify a failure. A per-board suite, one test row per board, is the pattern
the health check uses; the example's `conftest.py` shows it in ten lines.

## 6. Run it

**Runs → New run**: the project, the bundle, the boards; **Run**. Or
with a key:

```bash
curl -fsS -X POST -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  https://<rig>/api/v1/suites \
  -d '{"profile": "esp32-rig-example", "ref": "main", "artifact": "<bundle id>"}'
```

`ref` names the commit or branch the suite is checked out at; `artifact`
a bundle the rig holds (or leave it out to take the newest one it holds for
that commit); `targets` a subset of families; `tests` a list of test files or
ids; `keyword` a pytest `-k` expression. The answer is the run's id; its
page, and `GET /api/v1/jobs/<id>`, follow it.

## What a project cannot do

Run on a rig its owner did not add it to. Make the rig build. Reach another
project's bundles or runs. A profile's commands run with the rig's own
privileges: a rig runs projects its owner trusts, which is the point of it
being yours.
