# Shared reference

What every job on this repository draws on. Each file points at the repository file that owns a fact; follow the pointer for the detail, and when the two disagree the owning file is right.

| File | Answers |
|---|---|
| [architecture.md](architecture.md) | the map: the four repositories, the two halves of `alteriom_hil`, where to look for each question |
| [build-and-test.md](build-and-test.md) | how to install and run what CI runs, Linux versus Windows, the container recipe, the tests that pin files, what counts as evidence |
| [rules.md](rules.md) | the rules that are easy to break (tests first, boundaries, scope, secrets, families) and the house conventions, each with its owner |
| [release.md](release.md) | how a release is numbered, cut by a person and carried; moving the firmware pin; who follows a release |
| [documents.md](documents.md) | every document a person reads here, the guide first, who owns its facts, and the known drift |

Each stage's contract names the files it loads.
