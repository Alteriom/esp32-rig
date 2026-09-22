"""Which queued runs start, on which boards, and why the rest wait.

alteriom_hil.allocation decides without touching anything, so every rule the
farm schedules by is pinned here rather than discovered on the rig: nothing
jumps a job queued first, a run that is not concurrent has the rig to itself,
and a waiting job always says what it is waiting for.
"""

from alteriom_hil.allocation import RESOURCES, Demand, Grant, plan

BOARDS = [
    {"id": "esp32-01", "target": "esp32"},
    {"id": "esp32-02", "target": "esp32"},
    {"id": "c3-01", "target": "esp32-c3", "tags": ["psram"]},
    {"id": "c3-02", "target": "esp32-c3"},
    {"id": "s3-01", "target": "esp32-s3", "tags": ["psram"]},
    {"id": "c6-01", "target": "esp32-c6"},
    {"id": "esp8266-01", "target": "esp8266"},
]


def job(n: int) -> str:
    return f"{n:032x}"


def needs(n, *pairs, concurrent=True, resources=(), label=None):
    return Demand(
        job_id=job(n), label=label or f"project {n}", concurrent=concurrent,
        needs=tuple({"target": target, "count": count} for target, count in pairs),
        resources=frozenset(resources),
    )


def whole(n, label="painlessMesh"):
    return Demand(job_id=job(n), label=label, whole_rig=True)


def started(result):
    return [grant.job_id for grant in result.start]


def test_at_concurrency_one_every_run_has_the_rig_to_itself_in_queue_order():
    # How the farm always ran: the first job starts, alone, and the rest wait
    # in order -- even two jobs that would never touch the same board.
    queue = [needs(1, ("esp32-c3", 1)), needs(2, ("esp8266", 1))]
    result = plan(queue, BOARDS, [], limit=1)
    assert started(result) == [job(1)]
    grant = result.start[0]
    assert grant.shared is False, "alone: the rig lock exclusively"
    assert grant.boards == ("c3-01",), "it holds the boards it was given, not the bank"
    assert grant.resources == frozenset(RESOURCES)
    assert result.waiting[job(2)] == "waiting for the rig: run 00000000 (project 1) has it to itself"


def test_a_whole_bank_run_holds_every_board_and_discovery_holds_none():
    result = plan([whole(1)], BOARDS, [], limit=4)
    assert result.start[0].boards == tuple(board["id"] for board in BOARDS)
    assert result.start[0].whole_rig and not result.start[0].shared
    discovery = Demand(job_id=job(2), label="discovery", kind="inventory", whole_rig=True, holds_boards=False)
    assert plan([discovery], BOARDS, [], limit=4).start[0].boards == ()


def test_concurrent_runs_on_different_boards_start_together():
    queue = [needs(1, ("esp32-c3", 1)), needs(2, ("esp32", 2)), needs(3, ("esp8266", 1))]
    result = plan(queue, BOARDS, [], limit=3)
    assert started(result) == [job(1), job(2), job(3)]
    assert [grant.boards for grant in result.start] == [("c3-01",), ("esp32-01", "esp32-02"), ("esp8266-01",)]
    assert all(grant.shared for grant in result.start)
    assert result.waiting == {}


def test_a_run_whose_boards_are_held_waits_and_says_by_whom():
    running = [Grant(job_id=job(9), label="Alteriom firmware", kind="suite",
                     boards=("esp32-01", "esp32-02"), resources=frozenset(), shared=True)]
    result = plan([needs(1, ("esp32", 1))], BOARDS, running, limit=2)
    assert started(result) == []
    assert result.waiting[job(1)] == (
        "waiting for 1 x esp32: in use by run 00000000 (Alteriom firmware)"
    )


def test_a_run_that_is_not_concurrent_waits_for_an_idle_rig_and_nothing_jumps_it():
    # Shared runs are in progress; a whole-bank run is next. It waits for the
    # rig to empty, and the concurrent run queued after it must not start in
    # the meantime -- or a stream of small runs would starve the mesh suite.
    running = [Grant(job_id=job(9), label="canary", kind="suite", boards=("c6-01",),
                     resources=frozenset(), shared=True)]
    queue = [whole(1), needs(2, ("esp8266", 1))]
    result = plan(queue, BOARDS, running, limit=4)
    assert started(result) == []
    assert result.waiting[job(1)].startswith("waiting for the rig to be idle: it runs alone")
    assert "queued behind run 00000000" in result.waiting[job(2)]
    assert "needs the rig to itself" in result.waiting[job(2)]


def test_nothing_starts_beside_a_run_that_has_the_rig_to_itself():
    running = [Grant(job_id=job(9), label="painlessMesh", kind="suite", boards=("esp32-01",),
                     resources=frozenset(RESOURCES), shared=False, whole_rig=True)]
    result = plan([needs(1, ("esp8266", 1))], BOARDS, running, limit=4)
    assert started(result) == []
    assert result.waiting[job(1)] == "waiting for the rig: run 00000000 (painlessMesh) has it to itself"


def test_a_waiting_run_keeps_its_families_but_not_the_rest_of_the_rig():
    # The first job wants both C3s; one is busy. The second also wants a C3
    # and must not take the one that frees first -- but the third, which
    # wants an S3, is no business of the first and starts.
    running = [Grant(job_id=job(9), label="canary", kind="suite", boards=("c3-02",),
                     resources=frozenset(), shared=True)]
    queue = [needs(1, ("esp32-c3", 2)), needs(2, ("esp32-c3", 1)), needs(3, ("esp32-s3", 1))]
    result = plan(queue, BOARDS, running, limit=4)
    assert started(result) == [job(3)]
    assert result.waiting[job(1)].startswith("waiting for 2 x esp32-c3: in use by run 00000000 (canary)")
    assert result.waiting[job(2)] == (
        "queued behind run 00000000 (project 1), which was queued first and is waiting for esp32-c3 boards"
    )


def test_resources_are_held_like_boards():
    queue = [
        needs(1, ("esp32", 1), resources=["gateway"]),
        needs(2, ("esp32-c3", 1), resources=["gateway", "mqtt"]),
        needs(3, ("esp8266", 1)),
    ]
    result = plan(queue, BOARDS, [], limit=4)
    assert started(result) == [job(1), job(3)]
    assert result.waiting[job(2)] == "waiting for the gateway: run 00000000 (project 1) uses it"


def test_no_more_than_the_limit_run_at_once():
    queue = [needs(1, ("esp32", 1)), needs(2, ("esp32-c3", 1)), needs(3, ("esp8266", 1))]
    result = plan(queue, BOARDS, [], limit=2)
    assert started(result) == [job(1), job(2)]
    assert result.waiting[job(3)] == "waiting for a free slot: 2 of 2 runs are in progress"


def test_a_paused_queue_starts_nothing_and_says_so():
    result = plan([needs(1, ("esp32", 1)), whole(2)], BOARDS, [], limit=4, paused=True)
    assert result.start == []
    assert set(result.waiting.values()) == {"the queue is paused"}


def test_a_demand_the_rig_cannot_meet_starts_alone_on_an_idle_rig_to_rediscover():
    # Two S3s asked of a rig with one. Waiting forever says nothing; on an
    # idle rig it starts with the rig to itself, rediscovers, and either
    # finds the board or fails saying exactly what is short.
    result = plan([needs(1, ("esp32-s3", 2))], BOARDS, [], limit=4)
    assert started(result) == [job(1)]
    assert result.start[0].shared is False and result.start[0].boards == ()
    running = [Grant(job_id=job(9), label="canary", kind="suite", boards=("c6-01",),
                     resources=frozenset(), shared=True)]
    busy = plan([needs(1, ("esp32-s3", 2)), needs(2, ("esp8266", 1))], BOARDS, running, limit=4)
    assert started(busy) == []
    assert busy.waiting[job(1)] == (
        "waiting for the rig to be idle to rediscover it: needs 2 x esp32-s3, 1 connected"
    )
    assert "queued behind" in busy.waiting[job(2)]


def test_tags_narrow_a_family_to_the_boards_that_carry_them():
    tagged = Demand(job_id=job(1), label="psram", concurrent=True,
                    needs=({"target": "esp32-c3", "count": 1, "tags": ["psram"]},))
    assert plan([tagged], BOARDS, [], limit=2).start[0].boards == ("c3-01",)
    running = [Grant(job_id=job(9), label="canary", kind="suite", boards=("c3-01",),
                     resources=frozenset(), shared=True)]
    result = plan([tagged], BOARDS, running, limit=2)
    assert result.waiting[job(1)] == "waiting for 1 x esp32-c3 tagged psram: in use by run 00000000 (canary)"


def test_a_run_that_named_its_boards_gets_exactly_those():
    named = Demand(job_id=job(1), label="Farm canary", concurrent=True, boards=("c6-01", "esp8266-01"))
    assert plan([named], BOARDS, [], limit=2).start[0].boards == ("c6-01", "esp8266-01")
    running = [Grant(job_id=job(9), label="Alteriom firmware", kind="suite", boards=("c6-01",),
                     resources=frozenset(), shared=True)]
    result = plan([named], BOARDS, running, limit=2)
    assert result.waiting[job(1)] == "waiting for c6-01: run 00000000 (Alteriom firmware) is using it"
    # A free board is still not free to take when a run queued earlier is
    # waiting for its family.
    busy_esp32 = [Grant(job_id=job(9), label="Alteriom firmware", kind="suite",
                        boards=("esp32-01", "esp32-02"), resources=frozenset(), shared=True)]
    ahead = plan([needs(2, ("esp32", 1), ("esp32-c6", 1)), named], BOARDS, busy_esp32, limit=4)
    assert ahead.waiting[job(1)] == (
        "queued behind run 00000000 (project 2), which was queued first and is waiting for c6-01"
    )


def test_a_board_held_out_of_the_pool_is_never_allocated_by_family_or_to_the_bank():
    held = [dict(board) for board in BOARDS]
    for board in held:
        if board["id"] == "c3-01":
            board["hold"] = {"state": "quarantined", "reason": "radio"}
        if board["id"] == "esp8266-01":
            board["hold"] = {"state": "reserved", "reason": "bench"}
    assert plan([needs(1, ("esp32-c3", 1))], held, [], limit=2).start[0].boards == ("c3-02",)
    bank = plan([whole(2)], held, [], limit=2).start[0].boards
    assert "c3-01" not in bank and "esp8266-01" not in bank and "c3-02" in bank

    # Two C3s cannot be had while one is quarantined: said, not waited on.
    busy = [Grant(job_id=job(9), label="canary", kind="suite", boards=("c6-01",), resources=frozenset(), shared=True)]
    short = plan([needs(3, ("esp32-c3", 2))], held, busy, limit=2)
    assert short.waiting[job(3)] == (
        "waiting for the rig to be idle to rediscover it: needs 2 x esp32-c3, 1 connected "
        "and not held out (c3-01 quarantined)"
    )

    # A health check that named the quarantined board gets it; nothing gets the
    # reserved one, and a run that named it waits without holding up the rest.
    check = Demand(job_id=job(4), label="Farm canary", boards=("c3-01",))
    assert plan([check], held, [], limit=2).start[0].boards == ("c3-01",)
    bench = Demand(job_id=job(5), label="Farm canary", concurrent=True, boards=("esp8266-01",))
    result = plan([bench, needs(6, ("esp32", 1))], held, [], limit=2)
    assert result.waiting[job(5)] == "waiting for esp8266-01: it is reserved (bench)"
    assert [grant.job_id for grant in result.start] == [job(6)]
    alone = Demand(job_id=job(7), label="Farm canary", boards=("esp8266-01",))
    assert plan([alone], held, [], limit=1).waiting[job(7)] == "waiting for esp8266-01: it is reserved (bench)"


# ---- several workers ---------------------------------------------------------

PI = [
    {"id": "pi-esp32-01", "target": "esp32", "worker": "esp32-hil"},
    {"id": "pi-esp32-02", "target": "esp32", "worker": "esp32-hil"},
    {"id": "pi-c3-01", "target": "esp32-c3", "worker": "esp32-hil"},
]
PI2 = [
    {"id": "pi2-esp32-01", "target": "esp32", "worker": "esp32-hil-2"},
    {"id": "pi2-c6-01", "target": "esp32-c6", "worker": "esp32-hil-2"},
]


def test_a_run_takes_its_boards_from_one_worker_never_across_two():
    # Two esp32 on the first Pi, one on the second: three are asked for, and
    # no single worker has three -- so it does not start with boards from both.
    result = plan([needs(1, ("esp32", 3))], PI + PI2, [], limits={"esp32-hil": 2, "esp32-hil-2": 2})
    assert result.start == [] or result.start[0].boards == (), "never spread across workers"
    # One esp32 and one C6 are only on the second Pi together.
    both = plan([needs(2, ("esp32", 1), ("esp32-c6", 1))], PI + PI2, [], limits={"esp32-hil": 2, "esp32-hil-2": 2})
    assert both.start[0].worker == "esp32-hil-2"
    assert set(both.start[0].boards) == {"pi2-esp32-01", "pi2-c6-01"}


def test_a_whole_bank_run_takes_one_workers_bank_and_leaves_the_other_worker_free():
    queue = [whole(1), needs(2, ("esp32", 1))]
    result = plan(queue, PI + PI2, [], limits={"esp32-hil": 2, "esp32-hil-2": 2})
    mesh, other = result.start
    assert mesh.worker == "esp32-hil" and set(mesh.boards) == {"pi-esp32-01", "pi-esp32-02", "pi-c3-01"}
    assert other.worker == "esp32-hil-2", "a run that has one worker to itself does not stop the other"


def test_each_worker_has_its_own_limit_and_a_busy_worker_sends_work_to_the_next():
    running = [Grant(job_id=job(9), label="canary", kind="suite", boards=("pi-esp32-01",),
                     resources=frozenset(), shared=True, worker="esp32-hil")]
    result = plan([needs(1, ("esp32", 1))], PI + PI2, running, limits={"esp32-hil": 1, "esp32-hil-2": 1})
    assert result.start[0].worker == "esp32-hil-2", "the first worker is at its limit"
    both_full = running + [Grant(job_id=job(8), label="canary", kind="suite", boards=("pi2-c6-01",),
                                 resources=frozenset(), shared=True, worker="esp32-hil-2")]
    waiting = plan([needs(1, ("esp32", 1))], PI + PI2, both_full, limits={"esp32-hil": 1, "esp32-hil-2": 1})
    assert waiting.waiting[job(1)] == (
        "waiting for esp32-hil to be idle: it runs alone, and run 00000000 (canary) is running"
    )


def test_a_worker_that_does_not_run_the_profile_is_never_offered_it():
    demand = Demand(job_id=job(1), label="Alteriom firmware", concurrent=True, profile="alteriom-firmware",
                    needs=({"target": "esp32", "count": 1},))
    profiles = {"esp32-hil": frozenset({"canary", "painlessmesh"}), "esp32-hil-2": frozenset({"alteriom-firmware"})}
    assert plan([demand], PI + PI2, [], limit=2, profiles=profiles).start[0].worker == "esp32-hil-2"
    nobody = {"esp32-hil": frozenset({"canary"}), "esp32-hil-2": frozenset({"canary"})}
    assert plan([demand], PI + PI2, [], limit=2, profiles=nobody).waiting[job(1)] == "no worker runs alteriom-firmware"


def test_a_worker_with_no_boards_yet_is_still_a_rig_with_a_limit():
    result = plan([needs(1, ("esp32", 1))], PI, [], limits={"esp32-hil": 1, "sim-01": 2})
    assert result.start[0].worker == "esp32-hil"
    assert Grant.from_dict(result.start[0].as_dict()) == result.start[0]
