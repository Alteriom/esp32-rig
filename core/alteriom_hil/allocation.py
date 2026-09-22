"""Which queued runs may start now, on which boards, and why the rest wait.

The farm used to run one job at a time under one lock. A run that needs one
C6 for four minutes waited behind a forty-minute mesh suite that would never
touch it (docs/farm-allocation.md). This is the decision that replaces "the
next job in the queue": given the queue in order, the boards the rig has, and
what the running jobs hold, it says which jobs start and on which boards, and
gives every job that does not a reason a person can act on.

It decides only. It touches no hardware, holds no lock and reads no file, so
every rule below is a test rather than something found on the rig.

The rules, in the order they are applied to each queued job:

1. **A paused queue starts nothing.**
2. **Nothing jumps a job that was queued first.** A job that cannot start
   keeps what it is waiting for from the jobs behind it: the whole rig if it
   needs the whole rig, otherwise the families, named boards and resources it
   asked for. A small job may start ahead of a big one only on boards and
   resources the big one does not want -- so the big one is never starved by a
   stream of small ones.
3. **A run that is not concurrent has the rig to itself.** It starts only on
   an idle rig, and nothing starts beside it. Every profile is this unless it
   says otherwise, and so is discovery: it opens every serial port.
4. **At most ``limit`` runs at once.** The host's `queue.concurrency`; at 1
   every run has the rig to itself, which is how the farm behaved before this.
5. **A concurrent run starts when its boards and resources are free.**
   Boards are chosen from the connected ones by family and tags, in the order
   the inventory lists them.

**A board held out of the pool is never allocated by family or to a whole-bank
run.** A board carries a ``hold`` when an operator reserved it for bench work
or the canary quarantined it for failing its own checks. A run that *named* a
quarantined board still gets it -- that is how a health check clears one -- but
a reserved board is nobody's until it is released.

A demand the rig cannot meet even when idle -- two C3s asked of a rig with one
-- starts anyway once the rig is idle, with the rig to itself: its discover
stage rediscovers the hardware, which finds a board that came back, or fails
the run saying exactly what is short. Waiting for it forever would say nothing.

**Several rigs.** Behind a portal (docs/portal-plan.md) the boards belong to
workers, each board carrying the ``worker`` it is on. Every rule above holds
per worker: a run's boards all come from one worker -- a mesh cannot span two
rooms' radios -- a run that is not concurrent has *its worker* to itself, and
each worker has its own limit. A queued job tries the workers that could take
it, in order, and starts on the first that can; one that cannot start keeps
what it waits for from the jobs behind it on each of those workers. A worker
that does not run a job's profile is never offered it. With no ``worker`` on
any board there is one rig, and this is exactly the single-rig behaviour.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace

# Rig-wide things a run can use that are not boards: the access point and its
# HTTP probe, and the broker beside it. Two concurrent runs that both declare
# one never overlap; a run that is not concurrent holds them all anyway.
RESOURCES = ("gateway", "mqtt")


@dataclass(frozen=True)
class Demand:
    """What one queued job asks of the rig."""

    job_id: str
    label: str
    kind: str = "suite"
    # Every board and every resource: an exclusive profile, or discovery.
    whole_rig: bool = False
    # May run beside other runs. Meaningless with whole_rig.
    concurrent: bool = False
    # Exactly these boards, when the request named them.
    boards: tuple[str, ...] = ()
    # Otherwise, by family: {"target": str, "count": int, "tags": [str, ...]}.
    needs: tuple[dict, ...] = ()
    resources: frozenset[str] = frozenset()
    # Discovery holds the rig without holding any board: nothing it touches is
    # a board another job could have had, and it marks none in use.
    holds_boards: bool = True
    # The profile it runs: a worker that does not have it is not offered it.
    profile: str | None = None


@dataclass(frozen=True)
class Grant:
    """What a started job holds until it ends."""

    job_id: str
    label: str
    kind: str
    boards: tuple[str, ...]
    resources: frozenset[str]
    # Shared: the rig lock taken shared and each board's own lock exclusively,
    # beside other shared runs. Otherwise the rig lock exclusively, alone.
    shared: bool
    whole_rig: bool = False
    since: str = ""
    # The worker whose boards these are; None on a farm that is one rig.
    worker: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "Grant":
        return cls(
            job_id=data["job_id"], label=data.get("label") or "", kind=data.get("kind") or "suite",
            boards=tuple(data.get("boards") or ()), resources=frozenset(data.get("resources") or ()),
            shared=bool(data.get("shared")), whole_rig=bool(data.get("whole_rig")),
            since=data.get("since") or "", worker=data.get("worker"),
        )

    def as_dict(self) -> dict:
        return {
            "worker": self.worker,
            "job_id": self.job_id,
            "label": self.label,
            "kind": self.kind,
            "boards": list(self.boards),
            "resources": sorted(self.resources),
            "shared": self.shared,
            "whole_rig": self.whole_rig,
            "since": self.since or None,
        }


@dataclass
class Plan:
    start: list[Grant] = field(default_factory=list)
    # job id -> why it did not start
    waiting: dict[str, str] = field(default_factory=dict)


def _short(job_id: str) -> str:
    return job_id[:8]


def _held(grant: Grant) -> str:
    return f"run {_short(grant.job_id)} ({grant.label})"


def _matches(board: dict, need: dict) -> bool:
    return (
        not board.get("hold")
        and board.get("target") == need["target"]
        and set(need.get("tags") or ()) <= set(board.get("tags") or ())
    )


def _hold(board: dict) -> str | None:
    hold = board.get("hold") or {}
    return hold.get("state")


def _need_text(need: dict) -> str:
    tags = need.get("tags") or ()
    return f"{need['count']} x {need['target']}" + (f" tagged {', '.join(tags)}" if tags else "")


def _fits_idle(demand: Demand, boards: list[dict]) -> bool:
    """Whether the rig as connected could meet this demand with nothing running."""
    ids = {board.get("id") for board in boards}
    if demand.boards:
        return set(demand.boards) <= ids
    pool = list(boards)
    for need in demand.needs:
        matching = [board for board in pool if _matches(board, need)]
        if len(matching) < need["count"]:
            return False
        for board in matching[: need["count"]]:
            pool.remove(board)
    return True


class _Rig:
    """One set of boards that a run's allocation must come from entirely."""

    def __init__(self, name: str | None, boards: list[dict], active: list[Grant], limit: int,
                 profiles: frozenset[str] | None, alone_in_farm: bool):
        self.name = name
        self.boards = boards
        self.active = active
        self.limit = max(1, int(limit))
        self.profiles = profiles
        self.alone_in_farm = alone_in_farm
        self.word = "the rig" if name is None else name
        # What the jobs ahead in the queue are still waiting for, here.
        self.blocked_whole: Demand | None = None
        self.blocked_targets: dict[str, Demand] = {}
        self.blocked_boards: dict[str, Demand] = {}
        self.blocked_resources: dict[str, Demand] = {}

    def runs_profile(self, demand: Demand) -> bool:
        return self.profiles is None or demand.profile is None or demand.profile in self.profiles

    def eligible(self, demand: Demand) -> bool:
        if not self.runs_profile(demand):
            return False
        if demand.boards:
            return set(demand.boards) <= {board.get("id") for board in self.boards}
        if demand.whole_rig:
            return bool(self.boards) or self.alone_in_farm
        return _fits_idle(demand, self.boards)

    def block(self, demand: Demand, whole: bool) -> None:
        if whole:
            self.blocked_whole = self.blocked_whole or demand
            return
        for need in demand.needs:
            self.blocked_targets.setdefault(need["target"], demand)
        for board_id in demand.boards:
            self.blocked_boards.setdefault(board_id, demand)
        for resource in demand.resources:
            self.blocked_resources.setdefault(resource, demand)

    def attempt(self, demand: Demand) -> Grant | tuple[str, bool]:
        """A grant on this rig, or why not and whether it holds the whole rig back."""
        boards, active, limit = self.boards, self.active, self.limit
        alone = demand.whole_rig or not demand.concurrent or limit == 1
        if self.blocked_whole is not None:
            first = self.blocked_whole
            return (f"queued behind run {_short(first.job_id)} ({first.label}), "
                    f"which was queued first and needs {self.word} to itself", alone)
        solo = next((grant for grant in active if not grant.shared), None)
        if solo is not None:
            return f"waiting for {self.word}: {_held(solo)} has it to itself", alone
        feasible = demand.whole_rig or _fits_idle(demand, boards)
        if alone or not feasible:
            if active:
                if feasible:
                    reason = (f"waiting for {self.word} to be idle: it runs alone, and "
                              f"{', '.join(_held(grant) for grant in active)} "
                              f"{'is' if len(active) == 1 else 'are'} running")
                else:
                    reason = (f"waiting for {self.word} to be idle to rediscover it: "
                              f"{_shortfall(demand, boards)}")
                return reason, True
            # An idle rig: it starts, alone.
            chosen = _choose(demand, boards, set(), set()) if feasible else None
            if isinstance(chosen, str):
                # A board it named was reserved after it was queued.
                return _explain(demand, chosen, boards, {}, {}, {}), False
            return Grant(
                job_id=demand.job_id,
                label=demand.label,
                kind=demand.kind,
                boards=tuple(
                    board.get("id") for board in boards if not _hold(board)
                ) if demand.whole_rig and demand.holds_boards else tuple(chosen or ()),
                resources=frozenset(RESOURCES),
                shared=False,
                whole_rig=demand.whole_rig,
                worker=self.name,
            )
        if len(active) >= limit:
            where = "" if self.name is None else f" on {self.name}"
            return f"waiting for a free slot{where}: {len(active)} of {limit} runs are in progress", False
        held_boards = {board_id: grant for grant in active for board_id in grant.boards}
        held_resources = {resource: grant for grant in active for resource in grant.resources}
        busy_resource = next((r for r in sorted(demand.resources) if r in held_resources), None)
        if busy_resource:
            return f"waiting for the {busy_resource}: {_held(held_resources[busy_resource])} uses it", False
        behind_resource = next((r for r in sorted(demand.resources) if r in self.blocked_resources), None)
        if behind_resource:
            first = self.blocked_resources[behind_resource]
            return (f"queued behind run {_short(first.job_id)} ({first.label}), which was queued "
                    f"first and is waiting for the {behind_resource}"), False
        chosen = _choose(demand, boards, set(held_boards), set(self.blocked_boards), self.blocked_targets)
        if isinstance(chosen, str):
            return _explain(demand, chosen, boards, held_boards, self.blocked_boards, self.blocked_targets), False
        return Grant(
            job_id=demand.job_id,
            label=demand.label,
            kind=demand.kind,
            boards=tuple(chosen),
            resources=frozenset(demand.resources),
            shared=True,
            worker=self.name,
        )


def plan(
    queue: list[Demand],
    boards: list[dict],
    running: list[Grant],
    limit: int = 1,
    paused: bool = False,
    limits: dict[str | None, int] | None = None,
    profiles: dict[str | None, frozenset[str]] | None = None,
) -> Plan:
    """Decide which of ``queue`` (in the order it would run) start now.

    ``boards`` are the connected boards, each a mapping with ``id``,
    ``target`` and optional ``tags``, ``hold`` and ``worker``; ``running``
    what started jobs hold. ``limits`` and ``profiles`` are per worker, for a
    worker that has its own limit or runs only some profiles; ``limit`` is
    the limit of a worker not named in ``limits``.
    """
    result = Plan()
    if paused:
        result.waiting = {demand.job_id: "the queue is paused" for demand in queue}
        return result
    names: list[str | None] = []
    for name in [board.get("worker") for board in boards] + [grant.worker for grant in running] + list(limits or {}):
        if name not in names:
            names.append(name)
    if not names:
        names = [None]
    alone_in_farm = len(names) == 1
    rigs = [
        _Rig(
            name,
            [board for board in boards if board.get("worker") == name],
            [grant for grant in running if grant.worker == name],
            (limits or {}).get(name, limit),
            (profiles or {}).get(name),
            alone_in_farm,
        )
        for name in names
    ]
    for demand in queue:
        candidates = [rig for rig in rigs if rig.eligible(demand)]
        if not candidates:
            runners = [rig for rig in rigs if rig.runs_profile(demand)]
            if not runners:
                result.waiting[demand.job_id] = f"no worker runs {demand.profile or demand.label}"
                continue
            # Nothing could meet it even idle: the first worker that runs it
            # takes it alone, once idle, to rediscover (see above).
            candidates = runners[:1]
        outcomes = []
        for rig in candidates:
            outcome = rig.attempt(demand)
            if isinstance(outcome, Grant):
                result.start.append(outcome)
                rig.active.append(outcome)
                break
            outcomes.append((rig, outcome))
        else:
            result.waiting[demand.job_id] = outcomes[0][1][0]
            for rig, (_, whole) in outcomes:
                rig.block(demand, whole)
    return result


def _choose(
    demand: Demand,
    boards: list[dict],
    held: set[str],
    reserved_ahead: set[str],
    targets_ahead: dict[str, Demand] | None = None,
) -> list[str] | str:
    """The boards this demand gets, or the family/board it is short of."""
    targets_ahead = targets_ahead or {}
    if demand.boards:
        reserved = {board.get("id") for board in boards if _hold(board) == "reserved"}
        for board_id in demand.boards:
            if board_id in held or board_id in reserved_ahead or board_id in reserved:
                return board_id
        taken = {board.get("id") for board in boards if board.get("id") in demand.boards}
        for board in boards:
            if board.get("id") in taken and board.get("target") in targets_ahead:
                return board["id"]
        return sorted(taken)
    chosen: list[str] = []
    for need in demand.needs:
        if need["target"] in targets_ahead:
            return need["target"]
        free = [
            board.get("id") for board in boards
            if _matches(board, need) and board.get("id") not in held
            and board.get("id") not in reserved_ahead and board.get("id") not in chosen
        ]
        if len(free) < need["count"]:
            return need["target"]
        chosen.extend(free[: need["count"]])
    return chosen


def _shortfall(demand: Demand, boards: list[dict]) -> str:
    if demand.boards:
        ids = {board.get("id") for board in boards}
        gone = sorted(set(demand.boards) - ids)
        return f"{', '.join(gone)} {'is' if len(gone) == 1 else 'are'} not connected"
    connected = Counter(board.get("target") for board in boards)
    short = []
    for need in demand.needs:
        free = sum(1 for board in boards if _matches(board, need))
        if free >= need["count"]:
            continue
        held_out = sorted(
            f"{board.get('id')} {_hold(board)}" for board in boards
            if _hold(board) and _matches({**board, "hold": None}, need)
        )
        short.append(
            f"needs {_need_text(need)}, {free} connected"
            + (f" and not held out ({', '.join(held_out)})" if held_out else "")
        )
    return "; ".join(short) or f"the rig has {dict(connected)}"


def _explain(
    demand: Demand,
    short: str,
    boards: list[dict],
    held: dict[str, Grant],
    reserved_ahead: dict[str, Demand],
    targets_ahead: dict[str, Demand],
) -> str:
    if demand.boards:
        if short in held:
            return f"waiting for {short}: {_held(held[short])} is using it"
        board = next((item for item in boards if item.get("id") == short), {})
        if _hold(board) == "reserved":
            reason = (board.get("hold") or {}).get("reason")
            return f"waiting for {short}: it is reserved" + (f" ({reason})" if reason else "")
        first = reserved_ahead.get(short) or targets_ahead.get(
            next((board.get("target") for board in boards if board.get("id") == short), ""), None
        )
        if first is not None:
            return (f"queued behind run {_short(first.job_id)} ({first.label}), which was queued "
                    f"first and is waiting for {short}")
        return f"waiting for {short}"
    if short in targets_ahead:
        first = targets_ahead[short]
        return (f"queued behind run {_short(first.job_id)} ({first.label}), which was queued "
                f"first and is waiting for {short} boards")
    need = next(need for need in demand.needs if need["target"] == short)
    users = sorted({
        _held(grant) for board_id, grant in held.items()
        if any(board.get("id") == board_id and _matches(board, need) for board in boards)
    })
    return (f"waiting for {_need_text(need)}: "
            + (f"in use by {', '.join(users)}" if users else "not enough free"))


def with_since(grant: Grant, since: str) -> Grant:
    return replace(grant, since=since)
