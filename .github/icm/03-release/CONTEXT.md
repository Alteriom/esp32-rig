# 03-release — the pipeline

The tag is the version: plan it, let a person push it, check what was attached, then bring the consumers along.

| Stage | Job | Input | Output | Human check |
|---|---|---|---|---|
| [`01_plan`](stages/01_plan/CONTEXT.md) | which commit, which number | the changes since the last release | `output/plan.md` | A person approves the tag number and the commit; nothing is tagged until they do. |
| [`02_publish`](stages/02_publish/CONTEXT.md) | a person tags, the release is checked | 01_plan's `plan.md` | `output/publish.md` | A person pushes the tag, then reads `publish.md` and confirms the release page shows every asset before anyone is told to take it. |
| [`03_follow`](stages/03_follow/CONTEXT.md) | bring the consumers along | 02_publish's `publish.md` | `output/follow.md` | A person reads `follow.md` and opens (or asks for) each consumer's pull request; a change to `core/` or `rig/` is done when the farm portal's mirror has landed. |
