# Contributing

This is the software for a hardware test rig. Most of it cannot be proved by
reading it, so the bar here is evidence rather than review.

## What a change needs

**A test that fails without it.** Not a test that passes with it — one that
fails on the tree before your change and passes after. If you cannot write
that, say so in the pull request and say what you did instead.

**The reason in the code.** Comments here say *why*, with the date and what
was observed: "a rig measured 4.63–4.79 V against a 4.8 V threshold and
flipped between readings seconds apart". Six months later that is the only
thing that tells somebody whether your workaround is still needed. A comment
that restates the line below it is noise; delete it.

**Nothing about your rig in particular.** Host names, addresses, people, the
portal you happen to run: none of them ship. `tests/test_public_scrub.py`
checks this and will fail the build.

## Running the tests

```bash
pip install -e ./core[dev] -e ./rig[dev]
python -m pytest tests -q
```

Most of the suite runs anywhere. The parts that need hardware are marked, and
the parts that need Linux are skipped elsewhere — which means a green run on
macOS or Windows proves less than you think. CI runs Linux.

Suites can run against a **simulator** instead of boards
(`ALTERIOM_HIL_MODE=sim`): a board that boots, answers, holds a value in flash
and can be reset. It is not a substitute for hardware and is not treated as
one; it is how a change to the pipeline is tested without holding the rig.

## The boundaries, and why they are tests

Two files enforce what would otherwise be intentions:

- `tests/test_rig_package.py` — what may import what. A rig does not import
  the portal; the core imports neither half. The lists in it are ratchets:
  they shrink and cannot grow.
- `tests/test_public_scrub.py` — what may ship.

If one of them fails, it is usually right. If it is wrong, change the list and
say why in the same commit.

## Adding a board family

A family is a description — pins, the chip its flasher expects, how it is
reset — under `core/alteriom_hil/devices/families/`. If it needs code, the
description is wrong or the abstraction is; say which in the pull request.

## Reporting something that breaks on hardware

Include the run's evidence directory if you have one: the serial capture is
usually the whole answer, and a guess about cables without it is not a report.
