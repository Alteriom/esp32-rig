# The farm's MCP server

An assistant that can read the farm answers "what is the farm doing", "why did
that run fail", "which bundle did it flash" and "is this board healthy" from
the farm itself, rather than from somebody pasting dashboard pages into a
chat. `alteriom-farm-mcp` is that reader: a [Model Context
Protocol](https://modelcontextprotocol.io) server over stdio, which turns each
tool call into a read of the farm's API with a named key. It is step 6 of
read-only first.

## Read-only, on purpose

Every tool reads. None starts a run, cancels one, pins or deletes a bundle, or
touches a board — and the client it is built on
(`alteriom_hil.farm_client`, in the rig's core package) cannot
build a request that would. Tools that change the farm come after this has
been used, and will need a key whose role allows them: the farm refuses those
routes to a `user` key whatever a client asks
([keys and roles](dashboard.md#keys-and-roles)). A **`user` key is all
this needs**, and it is the key to give it — never the farm's token.

## Set it up

On the farm host, a key for the assistant:

```bash
sudo alteriom-hil-admin keys create --name assistant --role user --note "MCP"
```

On the machine the assistant runs on, the HAL installed (it brings the
`alteriom-farm-mcp` command) and the key in a file only you can read:

```bash
pip install -e ./hal
install -m 0600 /dev/null ~/.config/alteriom/farm-key && $EDITOR ~/.config/alteriom/farm-key
```

Then register it with the client. For Claude Code:

```bash
claude mcp add alteriom-farm \
  --env ALTERIOM_FARM_URL=https://hil.example.com \
  --env ALTERIOM_FARM_KEY_FILE=$HOME/.config/alteriom/farm-key \
  -- alteriom-farm-mcp
```

`ALTERIOM_FARM_URL` is where the dashboard is served — through the TLS proxy
or a tailnet ([reaching the dashboard](bringup.md#7-reach-the-dashboard)). The
client refuses plain `http` to anything but loopback, since a key sent in the
clear is a key given away. `ALTERIOM_FARM_KEY` works in place of the file, for
a client that injects secrets as environment.

## Tools

| Tool | Answers |
|---|---|
| `farm_whoami` | the name and role of the key it reads with |
| `farm_status` | health and what is wrong with it, the queue, boards connected and missing, the ten latest runs |
| `farm_devices` | every board — family, state, the run holding it, its last Rig Health Check verdict (the `canary` profile) and what failed — plus missing and unregistered devices and instruments |
| `farm_capacity` | boards per family — connected, free, in use, and the tags the free ones carry — and how many runs may run at once, are running and wait: whether a run asking for one esp32-c3 would start now |
| `farm_runs` | runs newest first, filtered by status, kind or a search |
| `farm_run` | one run's stages and where it stopped, the failure detail, each capability's verdict, the failed tests, its bundle and its evidence |
| `farm_run_log` | the last lines of a run's log |
| `farm_bundles` | the bundles the farm holds, by profile or search |
| `farm_bundle` | one bundle: manifest, images, files, the runs that flashed it |
| `farm_statistics` | runs and pass rate, run time, queue wait, rig busy, failures by stage, per project |

Results are cut to what a question needs — a run's page is tens of kilobytes of
report and log, and `farm_run` returns its stages, verdicts and failures, with
`farm_run_log` for the log — and capped at 60 KB. A wrong argument, a key the
farm refuses or a farm that cannot be reached comes back as a result the model
can read (`isError`), not a broken session.

## Protocol

JSON-RPC 2.0, one message per line on stdin and stdout, logging on stderr.
`initialize` (protocol versions 2025-06-18, 2025-03-26 and 2024-11-05),
`ping`, `tools/list` and `tools/call`; notifications are accepted and not
answered. Written against the protocol directly rather than an SDK: the HAL
supports Python 3.9 and the protocol this needs is small.
