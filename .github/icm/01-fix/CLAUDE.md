# 01-fix — fix a defect

One run is one defect: reproduced, fixed at its cause, and proposed as a pull request with the evidence. A fix to `core/` or `rig/` reaches a rig only through a release (`03-release`), and the farm portal only through its mirror of that release.

Stages, in order; inputs, outputs and checks for each are in [CONTEXT.md](CONTEXT.md):

1. [Reproduce](stages/01_reproduce/CONTEXT.md) — make it fail on purpose.
2. [Fix](stages/02_fix/CONTEXT.md) — change the cause, watch the reproduction pass.
3. [Verify](stages/03_verify/CONTEXT.md) — the checks CI will run, the records, the pull request.
