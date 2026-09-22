#!/usr/bin/env python3
"""The farm service as a program: `alteriom-hil-service`.

The service itself is `alteriom_hil.service` -- the base and the HTTP
surface, core, importing neither half. This is what knows which halves are
installed: the rig's, which is in the same distribution as this file, and
the portal's, which is a distribution of its own that a rig need not have.

So a rig installs alteriom-hil-core and alteriom-hil and can run as a
standalone farm or as a node; a host that also has alteriom-hil-portal can
run as a portal, and its standalone farm carries the portal's side as every
farm did before there was a portal (docs/public-release-plan.md, step 12e).
"""

from __future__ import annotations

import os
from pathlib import Path

from alteriom_hil import service
from alteriom_hil.service import *  # noqa: F401,F403 -- the service, whole, as this module has always published it
from alteriom_hil.service import (  # noqa: F401 -- what `import *` leaves behind
    _SECRET_QUERY_IN_LOG,
    _backup_summary,
    _delivery_summary,
    _https_repo,
    _notify_channels_of,
    _reject_missing_ref,
    _with_active,
)
from alteriom_hil.rig_manager import (  # noqa: F401 -- re-exported
    RigMixin,
    FARM_OWNED_ENV_KEYS,
    RIG_OWNED_ENV_PREFIXES,
    RUN_KINDS,
    RUN_KIND_ENV_KEY,
    ScrubbedLog,
    TEARDOWN_GRACE_SECONDS,
    _STATE_FILE_LOCK,
    _plural,
    _pump_output,
    _runtime_env_keys,
    refused_suite_env,
)

# The portal's half is asked for, not assumed: a rig is complete without it,
# and after the split it is not in the repository this file lives in. Absent,
# `compose` builds a standalone farm from the rig's half alone and the base
# refuses every portal method the way a node does.
try:
    from alteriom_hil.portal_manager import PortalMixin
except ImportError:  # pragma: no cover - exercised by test_rig_package
    PortalMixin = None


# The class each mode runs as, composed once from the halves installed beside
# the base. Published here because this is where a farm's classes have always
# been found -- `FarmManager` is what a test builds, and it is the same object
# the service runs.
MANAGERS = service.compose(rig=RigMixin, portal=PortalMixin)
FarmManager = MANAGERS.get("standalone")
RigManager = MANAGERS.get("node")
PortalManager = MANAGERS.get("portal")
MANAGER_FOR_MODE = MANAGERS


def manager_for(mode: str):
    """The class a mode runs as. A rig carries no portal code and a portal no
    driver; a standalone farm is both when both are installed."""
    return MANAGERS[mode]


def web_root() -> Path | None:
    """The rig's dashboard bundle, when this is running from a checkout.

    `rig/web`, beside the package this file is in. A host passes `--web-root`
    -- every unit and the image do -- and does not rely on this; an installed
    wheel has no bundle beside it until a release carries one (step 13).
    """
    found = Path(__file__).resolve().parents[1] / "web"
    return found if found.is_dir() else None


def start_agent(manager, args) -> None:
    """What a node does once its manager exists: take work from its portal.

    The agent is the rig's -- the base has none -- so it is started from
    here and the service knows it only as something to call.
    """
    from alteriom_hil import farm_node

    client = farm_node.PortalClient(args.portal_url,
                                    args.node_key_file.read_text(encoding="utf-8").strip())
    # A node that is its host's farm installs the portal's releases; one
    # attached beside a standalone service is updated with it, by its own
    # deploy (runner/install-health-service.sh).
    attached = os.environ.get("ALTERIOM_HIL_FARM_ATTACHED") == "1"
    farm_node.NodeAgent(
        manager, client, args.worker_name,
        releases=None if attached else Path(args.state) / "update",
    ).start()


def main(argv: list[str] | None = None) -> int:
    return service.serve(argv, classes=MANAGERS, agent=start_agent, web_root=web_root())


if __name__ == "__main__":
    raise SystemExit(main())
