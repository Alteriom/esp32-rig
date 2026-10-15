# 02-feature — the pipeline

Say what changes for whom, prove it with a test that fails first, bring the guide along.

| Stage | Job | Input | Output | Human check |
|---|---|---|---|---|
| [`01_spec`](stages/01_spec/CONTEXT.md) | what changes, for whom | the request | `output/spec.md` | A person approves the spec, or corrects it, before any code is written. |
| [`02_implement`](stages/02_implement/CONTEXT.md) | the failing test, then the change | 01_spec's `spec.md` | `output/change.md` | A person reads the diff beside the spec: it does what the spec says and no more, the test failed first, and no rule was loosened. |
| [`03_verify`](stages/03_verify/CONTEXT.md) | the checks, the guide, the pull request | 02_implement's `change.md` | `output/pull-request.md` | A person reads `pull-request.md`, the guide diff and the evidence, and says whether to open it; the reviewer, not the author, merges. |
