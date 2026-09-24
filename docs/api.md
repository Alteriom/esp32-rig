# API reference

Everything the dashboard does, it does through this API, so anything you can
click you can script. JSON in and out, under `/api/v1/` on the rig (port 8090
on the host, or behind its proxy). Authenticate with a key in the
`Authorization: Bearer <key>` header. Roles: `user` reads and submits,
`admin` changes, `node` is a farm's agent.

```bash
KEY=$(sudo cat /etc/alteriom-hil/api-token)
curl -fsS -H "Authorization: Bearer $KEY" http://127.0.0.1:8090/api/v1/view | python3 -m json.tool
```

An error is `{"error": "<what went wrong, in words>"}` with a 4xx status; a
refusal by GitHub is passed through as GitHub said it.

## The rig

| | Role | Does |
|---|---|---|
| `GET /api/v1/view` | user | **the rig view**: one document with the rig's name, description, location, version, health, boards, inventory, running jobs, profiles, setup state. Contract 1. What the dashboard's rig page renders, and what a farm shows for this rig |
| `GET /api/v1/status` | user | the service's status: mode, version, queue, inventory, health, who the key is |
| `GET /api/v1/whoami` | any key | the key's name and role |
| `GET /api/v1/rig/details` | user | name, description, location |
| `POST /api/v1/rig/details` | admin | set them: `{"name", "description", "location"}` |
| `GET /api/v1/farm/public` | user | the public page of the farm the overview shows, cached; `farm.public_url: off` answers none |
| `GET /api/v1/inventory` | user | the boards and instruments as last discovered |
| `POST /api/v1/inventory/refresh` | admin | rediscover (waits for an idle rig) |
| `POST /api/v1/health` | admin | run the Rig Health Check: `{"targets": [...]}` or `{"boards": [...]}` to narrow |

## Projects

| | Role | Does |
|---|---|---|
| `GET /api/v1/projects` | user | the projects on this rig, each with its configuration, bundles and recent runs |
| `POST /api/v1/projects/inspect` | admin | `{"repo": "<url>"}`: what GitHub says about the repository, as a filled-in form with what was found and what was guessed |
| `POST /api/v1/projects` | admin | add one: `{"name", "repo", "default_ref", "suite_path", "families", "supply_workflow", "supply_artifact", "revision_key", "min_boards", "timeout_seconds"}`; everything but `repo` has a default; refused when the token cannot read the repository |
| `POST /api/v1/projects/<name>` | admin | change one, same fields |
| `POST /api/v1/projects/<name>/delete` | admin | remove it (a shipped one is tombstoned) |
| `POST /api/v1/projects/<name>/restore` | admin | bring a removed shipped project back |
| `POST /api/v1/projects/<name>/fetch` | admin | **Get firmware from GitHub**: the newest artifact of the supply workflow, checked and held; `{"reused": true}` when the rig already holds that commit |
| `POST /api/v1/projects/<name>/runs/delete` | admin | delete every finished run of the project |
| `GET /api/v1/github` | user | whether a token is set, who GitHub says it is, when it was last checked |
| `POST /api/v1/github` | admin | `{"token": "..."}`: store a token (checked with GitHub first); never shown back |
| `POST /api/v1/github/remove` | admin | forget it |

## Runs

| | Role | Does |
|---|---|---|
| `POST /api/v1/suites` | user | submit a run: `{"profile", "ref", "artifact", "targets", "boards", "tests", "keyword", "reuse", "supersede", "env", "actor"}`. Only `profile` matters; `ref` defaults to the project's default branch, `artifact` to the newest bundle the rig holds for that commit (none held: refused at submit, naming the workflow). Answers the run's id |
| `GET /api/v1/jobs` | user | runs, newest first; `?profile=`, `?state=`, `?limit=` |
| `GET /api/v1/jobs/<id>` | user | one run: state, stages with durations, results, the files it left |
| `GET /api/v1/jobs/<id>/artifacts/<name>` | user | one file a run left: the pipeline log, a board's serial capture, `results.xml`, the report |
| `POST /api/v1/jobs/<id>/cancel` | user | stop a queued or running run |
| `POST /api/v1/jobs/<id>/delete` | admin | delete a finished run and what it left |

A run's id is 32 hex characters. States: `queued`, `running`, `passed`,
`failed`, `cancelled`, `error`.

## Bundles

| | Role | Does |
|---|---|---|
| `GET /api/v1/artifacts` | user | the bundles the rig holds, a page at a time: id, project, commit, families, size, origin, pinned; `?profile=`, `?branch=`, `?q=`, `?limit=` |
| `GET /api/v1/artifacts/library` | user | the same, grouped by project and commit |
| `POST /api/v1/artifacts?profile=…&repo=…&workflow=…&run_id=…&run_url=…&commit=…` | user | hand a bundle over: the body is a `.tar.gz` of the bundle directory, at most 256 MB; the query names its provenance, checked against the project's supply block; the manifest's revision key must equal `commit`. Answers the bundle's id, or `{"reused": true, ...}` for a commit already held |
| `GET /api/v1/artifacts/<id>` | user | one bundle: its manifest and provenance |
| `GET /api/v1/artifacts/<id>/bundle` · `/files/<path>` | user | download it as `.tar.gz`, or one file out of it |
| `PATCH /api/v1/artifacts/<id>/pin` · `/unpin` | admin | keep it past every prune; stop |
| `PATCH /api/v1/artifacts/<id>` | admin | `{"delete": true}`: remove one |
| `PATCH /api/v1/artifacts/prune` | admin | remove what retention would, now |

## Boards

Boards live in the inventory, which is what discovery writes.

| | Role | Does |
|---|---|---|
| `GET /api/v1/inventory` | user | every board and instrument: port, family, MAC, state, last health verdicts |
| `GET /api/v1/inventory/<board>/details` | user | the chip as the board reported it |
| `GET /api/v1/inventory/<board>/history` | user | the runs and health checks it took part in |
| `PATCH /api/v1/inventory/<board>/reserve` · `/release` | admin | `{"reason"}`: hold a board out of runs, or release it (also how quarantine is lifted) |
| `PATCH /api/v1/inventory/register` | admin | register a board by hand, as `alteriom-hil-admin boards add` does |
| `POST /api/v1/health` | admin | the Rig Health Check: `{"boards": ["<id>"]}` for one board, or nothing for all |

## Queue

| | Role | Does |
|---|---|---|
| `PATCH /api/v1/queue/pause` | admin | `{"reason": "..."}`: stop starting runs |
| `PATCH /api/v1/queue/resume` | admin | start again (a passing health check does this too) |

## Host

| | Role | Does |
|---|---|---|
| `GET /api/v1/config` | admin | the host configuration, by section, with what each setting means |
| `GET /api/v1/stats` | user | runs, pass rates and durations over time |
| `GET /api/v1/storage` · `/storage/<kind>` | user | what the rig's disk holds, by kind |
| `GET /api/v1/retention` · `PATCH /api/v1/retention/run` | admin | what retention removes on its own; run it now |
| `GET /api/v1/setup` | user | what a new rig still has to do |
| `GET /api/v1/keys` | admin | who holds a key, by name and role; never a key |
| `GET /api/v1/sessions` · `POST /api/v1/sessions/revoke` | user | the browser sessions signed in with this key; end one |

Keys themselves are made and revoked on the host:

```bash
sudo alteriom-hil-admin keys create --name ci-bundles --role user    # printed once
sudo alteriom-hil-admin keys list
sudo alteriom-hil-admin keys revoke --name ci-bundles
```

## Webhooks

| | Role | Does |
|---|---|---|
| `GET /api/v1/webhooks` | admin | where this rig's events go; the secret is never among them |
| `PATCH /api/v1/webhooks` | admin | `{"url", "secret", "events": [...]}`: add one; deliveries are signed with the secret when given |
| `PATCH /api/v1/webhooks/<id>` · `…/<id>/test` · `DELETE /api/v1/webhooks/<id>` | admin | change, send a test event, remove |
| `GET /api/v1/webhooks/<id>/deliveries` | admin | what was sent and what came back |

The same events go to the channels `alteriom-hil-admin notify` sets up.

## The command line

`alteriom-hil-admin` on the host does what the API does and a little more,
under `sudo` for anything that writes:

| Command | Does |
|---|---|
| `status` | the saved or live health snapshot |
| `config show` · `validate` · `set <key> <value>` · `apply` | the host configuration |
| `config join --portal … --token …` | make this host a node of a farm |
| `github set` · `check` · `remove` | the GitHub token |
| `keys list` · `create` · `revoke` | API keys |
| `boards list` · `discover` · `add` · `remove` · `validate` | the board registry |
| `instruments list` · `add` · `wire` · `unwire` · `probe` · `remove` | test equipment and its jumpers |
| `health refresh` | a fresh host health snapshot |
| `upgrade [--from DIR] [--no-firmware] [--dry-run]` | install a release |
| `backup create` · `list` · `restore [--apply]` | the nightly backup, by hand |
| `notify set` · `show` · `test` · `tune` · `remove` | where the rig says it broke |
| `providers set` · `show` · `check` · `test` · `remove` | real services the rig validates with your credential |
| `service` | the Actions runner service, on a rig that has one |

## The MCP server

The rig also speaks the Model Context Protocol, so an assistant can ask it
what is running, read a failure, or submit a run with the same key and the
same roles. [MCP](mcp.md) has the tools and how to connect.
