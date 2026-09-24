# Connecting to a farm

A rig is complete on its own. Connecting it to a farm is a choice you make in
Settings, and one you can unmake.

## What a farm is

A farm is a portal that several rigs report to: one place with a page per
rig, the same page the rig shows on its own dashboard, and a queue it can
hand runs from. The rig software's own farm is the one it shows on its
overview out of the box, where you can see rigs of the same kind and how
they are doing. Nothing there is yours until you connect; `farm.public_url`
in the host configuration points the overview at another farm, or `off`
shows none.

## What connecting does

- **The farm gets this rig's page.** Its name, description and location as
  you wrote them in Settings → Rig; its boards and their health; its runs.
  The rig view is one JSON document (`GET /api/v1/view` on the rig, the same
  shape on the portal per rig), so the farm's page and the rig's own agree
  by construction.
- **The farm can hand it runs.** The rig's agent asks the portal for work,
  runs it on the boards, and reports back. A rig connected to a farm still
  runs its own projects from its own dashboard.
- **The farm can hand it releases.** A portal names the rig release its
  rigs run, and a connected rig installs it through the same path
  `alteriom-hil-admin upgrade` uses, pinned firmware included.
- **The farm does not build your firmware either**, and it does not run
  projects the rig's owner did not add: a rig runs its own projects and no
  other.

## How to connect

On the farm, **Rigs → Add rig** names the rig and gives one command to
paste on the rig's host. Its token works once, for an hour:

```bash
curl -fsSL https://<your-farm>/api/v1/join.sh | bash -s -- --portal https://<your-farm> --token afj_…
```

Run it as the user the rig runs as, sudo-capable, not root. It installs what
a rig needs from apt, trades the token for this rig's node key (kept in
`/etc/alteriom-hil/node-key`, readable by root and the rig's group), takes the
release the farm's rigs run, checks it against the digest the farm gave, and
installs the host as a node of that farm. The rig then says hello and appears
on the farm's **Rigs** page. Stopped halfway, it is re-run the same way: the
key it was given is kept, so the spent token is not needed again.

On a rig that is already installed, the same is done from **Settings → Rig
→ Connection to a farm**, or on the host:

```bash
sudo alteriom-hil-admin config join --portal https://<your-farm> --token afj_…
```

## What the farm sees, and who else does

A rig's visibility on the farm is the rig owner's setting: **private** (the
owner and the farm's admins), **shared** (signed-in users of the farm), or
**public** (the farm's public page shows the rig, its board families, its
health word and its run counts; never a repository, a commit, a test name, a
log or an artifact of a private project). Set it from the rig's page on the
farm, or `POST /api/v1/rigs/<name>/visibility` with the rig owner's key.

## Disconnecting

**Settings → Rig → Connection to a farm → Disconnect**, or on the host set
`farm.mode: standalone` and restart the service. The rig keeps its projects,
its runs and its boards; the farm keeps the history the rig reported while it
was connected, under the rig's name, until the farm's admin deletes the rig
there. Deleting a rig on the farm revokes its key; the host stops reporting
until it joins again.

## Modes, for the record

`farm.mode` in the host configuration:

| Mode | Meaning |
|---|---|
| `standalone` | the queue, the dashboard and the boards in this one service, as a rig begins |
| `node` | the rig works for a portal: runs and releases come from there; the dashboard is still the rig's own |
| `attached` | `standalone` with the node agent beside it, taking portal runs on the same boards; an accepted alias kept for one more release |
