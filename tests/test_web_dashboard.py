import json
import re
from pathlib import Path


# The dashboard is the rig's bundle and the site is the portal's; a portal
# serves both from one root (docs/public-release-plan.md, step 12d).
WEB = Path(__file__).resolve().parents[1] / "rig" / "web"
PORTAL_WEB = Path(__file__).resolve().parents[1] / "portal" / "web"
# The two halves of the manager: each in its own distribution now
# (docs/public-release-plan.md, step 12b), not beside the service.
RIG_HALF = Path(__file__).resolve().parents[1] / "rig" / "alteriom_hil" / "rig_manager.py"
PORTAL_HALF = Path(__file__).resolve().parents[1] / "portal" / "alteriom_hil" / "portal_manager.py"
# The service's own source: `alteriom_hil.service` in the core, which is
# where the routes and the handler live (docs/public-release-plan.md, 12c).
SERVICE = Path(__file__).resolve().parents[1] / "core" / "alteriom_hil" / "service.py"


def dashboard() -> str:
    """The dashboard's script: the rig's pages (app.js) and, where there is
    one, the portal's shell around them (portal-shell.js). An assertion about
    what the dashboard does reads both; one about where a thing lives reads
    one. A rig serves the first without the second -- `shell()` copes with its
    absence -- and so does the public repository, which is why a missing shell
    is nothing here rather than an error."""
    found = []
    for name in ("app.js", "portal-shell.js"):
        for root in (WEB, PORTAL_WEB):
            if (root / name).is_file():
                found.append((root / name).read_text(encoding="utf-8"))
                break
    return "\n".join(found)


def css_rules(style):
    """The stylesheet with its comments taken out, so an assertion about a
    rule is about a rule the browser will see.

    Every check here is a substring of the file, which cannot tell a rule from
    prose that happens to contain one: a comment left unclosed put four lines
    of English where a selector goes, the browser swallowed the rule after it,
    and the tabs stopped hiding anything at all -- with every test still
    passing. Stripping first, and refusing a file whose comments do not
    balance, is what makes those assertions mean something.
    """
    stripped = re.sub(r"/\*.*?\*/", "", style, flags=re.S)
    leftover = "*/" if "*/" in stripped else "/*" if "/*" in stripped else ""
    assert not leftover, (
        f"a comment in app.css is not closed ({leftover} is left over): the prose "
        "becomes a selector and the rule after it is swallowed"
    )
    return stripped


def test_live_polling_preserves_operator_position_and_pauses_when_hidden():
    script = (WEB / "app.js").read_text(encoding="utf-8")
    assert "setInterval" not in script
    assert "document.hidden" in script
    assert 'document.addEventListener("visibilitychange"' in script
    # A poll must not move the operator. showJob navigates only when asked,
    # and the poll refreshes the open run without asking -- asserted by the
    # intent rather than by the shape of the line, which it used to be.
    assert "if (focus) openRun(" in script
    assert "await showJob(selectedJobId)" in script
    assert script.count("scrollIntoView") == 1


def test_a_run_is_a_page_with_a_url_of_its_own():
    """A run is what an operator reads for twenty minutes, links to a
    colleague, and comes back to after a reload. All three want a URL; none
    of them wants the list of every other run scrolled above it."""
    page = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'data-page="run"' in page
    assert 'id="job-detail"' not in page, "the detail is a page, not a card in the list"
    # One router: a reload, a pasted link and the back button arrive the same
    # way, and the URL always says which run is on screen.
    assert "function parseRoute" in script and "function openRoute" in script
    assert 'window.addEventListener("popstate"' in script
    assert 'showPanel("run", updateHash, `/${route.id}`)' in script
    # The run page has no nav item, so the list it belongs to stays lit.
    assert 'target === "run" ? "runs"' in script
    # A link to a run the farm no longer has says so, rather than leaving the
    # last run's details under someone else's id.
    assert '"No such run"' in script


def test_dashboard_exposes_pipeline_navigation_and_simulator_evidence():
    page = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'data-panel="runs"' in page
    assert 'id="active-run"' in page
    assert 'id="simulation"' in page
    assert 'id="last-updated"' in page


def test_the_branch_column_shows_what_a_consumer_named_not_its_commit():
    """A consumer's CI validates a commit, so its ref is a SHA and the Branch
    column was blank for every run it sent. The branch it names travels in
    the request; the column prefers it, and still falls back to a ref that
    is not a commit for runs submitted by hand."""
    script = (WEB / "app.js").read_text(encoding="utf-8")
    body = script.split("function jobBranch(job)", 1)[1].split("\nfunction ", 1)[0]
    assert "job.request?.branch" in body
    assert body.index("job.request?.branch") < body.index("job.request?.ref")


def test_artifact_family_picker_is_built_from_the_service_not_hard_coded():
    """A family added to alteriom_hil.board.TARGET_CHIPS must appear in the
    dashboard's run form without a page edit: the service publishes its
    targets and the page renders them, pre-checking connected families."""
    page = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")
    service = SERVICE.read_text(encoding="utf-8")
    assert 'id="artifact-families"' in page
    assert 'name="target"' not in page, "family checkboxes are rendered by app.js"
    assert '"targets": sorted(TARGETS)' in service
    assert "renderFamilies(data.targets || [], inv)" in script
    assert "connected.has(target)" in script
    assert "familySignature" in script, "polling must not clobber operator toggles"


def test_overview_describes_the_fleet_by_family_and_the_queue_with_its_controls():
    """The overview answers "what does the farm have, and what is it doing":
    a table by chip family from the stored chip details, and a live pipeline
    card with a clock that ticks, the current stage, the boards held, and
    the queue in the worker's order with cancel, run-next, pause and resume."""
    page = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'id="hardware-overview"' in page
    assert "renderHardwareOverview" in script
    assert "data-timer-start" in script, "clocks tick between polls"
    assert "queue-toggle" in script and "/api/v1/queue/pause" in script and "/api/v1/queue/resume" in script
    assert "/promote" in script and "/cancel" in script
    assert "confirm(" in script, "cancelling a running run is confirmed"


def test_reports_are_rendered_from_markdown_and_never_carry_script():
    script = (WEB / "app.js").read_text(encoding="utf-8")
    assert "function renderMarkdown" in script
    assert "escapeHtml(source)" in script, "every line is escaped before markup is recognised"
    assert r"https?:\/\/" in script, "links are kept to http(s)"
    assert '<pre>${escapeHtml(job.report_markdown)}</pre>' not in script, "the report is no longer shown as source"


def test_artifacts_open_through_the_api_with_the_token_never_in_a_url():
    script = (WEB / "app.js").read_text(encoding="utf-8")
    service = SERVICE.read_text(encoding="utf-8")
    assert "/artifacts/" in script and "URL.createObjectURL" in script
    assert "token=" not in script.replace("farmToken", ""), "no token in a query string"
    assert "ARTIFACT_TYPES" in service and '"passwd"' not in service
    # Every file the run left is offered, not a fixed few: the serial
    # capture of every board and every family's flash image.
    assert 'name.startsWith("serial:")' in script and 'name.startsWith("firmware:")' in script
    assert 'glob("*.serial.log")' in service and 'glob("*/flash-image.bin")' in service


def test_the_artifacts_page_hands_every_bundle_back_and_cleans_only_on_confirmation():
    """Every bundle the farm keeps is listed and downloadable -- whole or file
    by file, through the API with the token -- and nothing is deleted without
    the operator confirming it: a prune is previewed first, a delete asks."""
    page = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'data-panel="artifacts"' in page and 'data-page="artifacts"' in page
    assert 'id="storage"' in page and 'id="bundles"' in page and 'id="prune-form"' in page
    assert "/api/v1/artifacts?" in script and "/api/v1/storage" in script
    assert "/bundle`" in script and "/files/" in script
    assert "dry_run: true" in script and "dry_run: false" in script, "a prune is previewed before it deletes"
    delete = script.split('querySelectorAll(".bundle-delete")', 1)[1].split("}));", 1)[0]
    assert "confirm(" in delete
    # The images can go while the note saying so fails to be written: the
    # operator is told, not left believing the runs will explain themselves.
    assert "removed?.record_error" in delete
    assert "token=" not in script.replace("farmToken", ""), "no token in a query string"
    # A run links to the bundle it flashed, and says so when it was pruned.
    assert "renderBundleNote(job)" in script and "removed_at" in script
    # A confirmation sends back what the preview showed; with no bundle to
    # delete, it can still tidy the links left pointing at nothing.
    assert "sendPrune(rules, bundles.map" in script and "sendPrune(rules, [])" in script and "prune-tidy" in script
    assert "dry_run: false" in script and "followPrune" in script, "a confirmed prune is followed, not waited on"
    # A prune another session started -- or this one before a reload -- is
    # followed by whoever loads the page, one follower at a time.
    assert "if (artifactIndex?.pruning?.active) followPrune()" in script
    assert "followingPrune" in script
    assert "preview.matched" in script, "a preview capped at what may be confirmed says how many more match"
    # A measurement that finishes while the tab is hidden is still picked up:
    # the poll waits rather than stopping.
    assert "function pollStorageSoon" in script and "return pollStorageSoon()" in script


def test_both_long_lists_are_paged_by_the_service_and_say_where_you_are():
    """The run history and the bundle store only grow. A client that fetched
    every row to filter it in the browser would get slower every week and
    then quietly stop showing the oldest -- which is exactly when somebody is
    looking for an old one. The service pages and filters both, and one pager
    renders both so they cannot drift into describing themselves
    differently."""
    page = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")
    service = SERVICE.read_text(encoding="utf-8")
    assert 'id="bundle-range"' in page and 'id="bundle-prev"' in page and 'id="bundle-next"' in page
    assert "function renderPager" in script
    # Both lists, through the one function.
    assert 'range: "job-range"' in script and 'range: "bundle-range"' in script
    # The filter goes to the service, because the client cannot filter what
    # it never fetched.
    assert "function bundleParams" in script
    assert 'params.set("q", bundleQuery.q)' in script and 'params.set("profile"' in script
    assert "ARTIFACT_PAGE_MAX" in service and 'one("q")' in service
    # A changed filter starts at its first page: page four of a search that
    # now matches two things is an empty table that looks broken.
    assert script.count("bundleQuery.offset = 0") >= 2
    # The totals stay the whole store's -- they are the disk story -- while
    # the pager counts through what the filter matched.
    assert '"matched": len(chosen)' in service and '"count": len(entries)' in service
    assert "index.count ?? 0" in script


def test_the_hardware_page_checks_a_board_with_the_canary_and_shows_its_verdict():
    """The farm's own firmware, run from the page: one board or all of them.

    A health check flashes the board, so it asks first; it is a queued run,
    so unlike a chip probe it does not need the rig free right now. And each
    board carries the canary's last verdict, because a board that started
    failing its radio join should be visible before it fails somebody's run.
    """
    page = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'id="check-all"' in page
    assert '"/api/v1/health"' in script
    assert "board-health" in script and "requestHealth" in script
    health = script.split("async function requestHealth", 1)[1].split("\n}", 1)[0]
    assert "confirm(" in health, "it flashes over whatever the board is carrying"
    # A farm with no canary shows no health actions rather than buttons that
    # cannot work -- and whether it has one is a profile, which the service
    # already reports.
    assert "function canaryAvailable" in script and "profiles.canary" in script
    # The verdict, and the distinction that makes it useful: a check red on
    # every board is the farm, not six broken boards.
    assert "function healthBadge" in script and "board.health" in script
    assert "farm_wide" in script and "FARM FAULT" in script


def test_a_shell_adds_its_own_settings_tabs_and_the_rig_adds_none():
    """Settings is General, Projects and Access, and whatever the shell around
    the dashboard adds -- a portal's Workspaces, for one. The markup is the
    rig's bundle and what a portal adds is the portal's, so the tab and its
    panel are built rather than written into index.html
    (docs/public-release-plan.md, phase 4).

    A rig adds none: what a workspace is, and who owns which rig, is a
    portal's question."""
    app = dashboard()
    assert "settingsTabs: []," in app, "the rig's shell adds no tabs"
    assert "settingsPanel: id => {}," in app

    # The list is the three plus the shell's, with Access last: it is the
    # admin's and reads as the end of the list.
    tabs = app.split("function settingsTabs()", 1)[1].split("\n}", 1)[0]
    assert '["general", "projects", ...shell().settingsTabs.map(tab => tab.id), "access"]' in tabs

    # A shell's tab may be the admin's alone, and says so the way every other
    # admin-only control does.
    assert 'if (tab.admin) link.classList.add("admin-only");' in app

    # Each is given a panel of its own, before the Access one so the order on
    # the page is the order in the nav.
    built = app.split("function buildSettingsTabs()", 1)[1].split("\n}\n", 1)[0]
    assert "nav.insertBefore(link, access)" in built
    assert 'panel.id = `settings-${tab.id}`' in built
    assert "$(\"settings-access\").parentNode.insertBefore(panel" in built

    # A tab the shell adds is asked for before anything knows which shell this
    # is -- the mode arrives with the status -- so a link straight to one is
    # remembered and shown once it does, rather than quietly becoming General.
    assert "let settingsWanted = null;" in app
    assert "settingsWanted = known || !tab ? null : tab;" in app
    assert "if (settingsWanted) showSettingsTab(settingsWanted);" in app

    # Showing one hides the others, whichever they are, and the shell fills it.
    showing = app.split("function showSettingsTab(", 1)[1].split("\n}\n", 1)[0]
    assert "for (const name of settingsTabs())" in showing
    assert "panel.hidden = name !== settingsTab" in showing
    assert "shell().settingsPanel(settingsTab)" in showing
    # And the three built-in panels are still in the markup, because they are
    # the dashboard's own.
    page = (WEB / "index.html").read_text(encoding="utf-8")
    for built_in in ("settings-general", "settings-projects", "settings-access"):
        assert f'id="{built_in}"' in page


def test_the_configuration_page_covers_every_parameter_the_host_can_set():
    """The page answers "what is this farm set to" so nobody needs an ssh
    session for it -- which only holds if it covers the whole configuration
    file. The rig's network was missing entirely, and that is the section
    that explains why the canary's radio, uplink and queue checks skip.

    Read from the schema rather than from a list kept here, so a parameter
    added to the host's configuration fails this test until the page shows
    it.
    """
    import json

    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "rig" / "hil-config.schema.json").read_text(encoding="utf-8")
    )
    script = (WEB / "app.js").read_text(encoding="utf-8")
    service = SERVICE.read_text(encoding="utf-8")

    def leaves(node, prefix=""):
        for name, child in (node.get("properties") or {}).items():
            if child.get("properties"):
                yield from leaves(child, f"{prefix}{name}.")
            else:
                yield f"{prefix}{name}"

    fields = set(leaves(schema))
    # `schema` is the file's own version; the service reports it as
    # host.config_schema, and `enabled` appears in three sections.
    assert "gateway.ssid" in fields and "mqtt.url" in fields, "the schema is the source"
    for field in sorted(fields):
        leaf = field.split(".")[-1]
        assert f"{leaf}:" in script or f'"{leaf}"' in script, (
            f"{field} is settable on the host and the configuration page "
            f"never shows it"
        )
        assert f'"{leaf}"' in service, f"the service does not report {field}"

    # The one thing deliberately absent: the secret. Its path is shown because
    # an operator needs to know which file to rotate, so `password_file` is
    # allowed and any other `gateway.passwordâ€¦` would be the password itself.
    assert "password_file" in script
    assert not re.search(r"gateway\.password(?!_file)", script)
    assert "Password file" in script

    # And what each profile is set to, which only the run form used to read.
    assert "function renderProfileTable" in script
    assert "supply_workflow" in script and "Firmware from" in script
    # A setting that is off is shown as off rather than dropped as though it
    # did not exist.
    assert "function settingValue" in script and 'return "off"' in script


def test_header_keeps_its_title_and_links_to_the_repository_and_deployed_commit():
    page = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")
    assert "<title>Alteriom ESP32 Farm</title>" in page
    assert "document.title" not in script, "the tab title never changes"
    assert 'id="repo-link"' in page and 'id="version"' in page
    assert "repositories" in script and "/commit/" in script


def test_the_run_form_offers_a_partial_run_and_reuse():
    """A debugging iteration runs the tests in question, not the suite: the
    form lists the suite's files from the service and takes a keyword
    expression. A finished run offers to re-run its failed tests or its
    selection, flashing the bundle the farm holds for the commit and skipping
    the flash when the boards already run it."""
    page = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")
    service = SERVICE.read_text(encoding="utf-8")
    assert 'id="test-files"' in page and 'name="keyword"' in page and 'id="bundle-select"' in page
    assert 'name="test"' not in page, "test files are rendered by app.js from the service"
    assert "renderSuiteTests(data.suite_tests" in script and '"suite_tests": manager.suite_catalogue()' in service
    assert "rerun-failed" in script and "rerun-same" in script
    assert "TEST_PATTERN" in service and "KEYWORD_PATTERN" in service
    pipeline = RIG_HALF.read_text(encoding="utf-8")
    assert "reusable_artifacts" in pipeline and "already_flashed" in pipeline
    rerun = script.split("async function rerun", 1)[1].split("\n}", 1)[0]
    assert "reuse: true" in rerun and "resolved_sha" in rerun


def test_a_bundle_is_a_page_and_says_whose_it_is():
    """A bundle's details used to open as a card under a table of forty
    bundles, and said "unknown" for every supplied one. It is a page now, like
    a run: linkable, and naming its project, repository, branch, the user who
    started its run and the CI run that built it."""
    page = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'data-page="artifact"' in page
    assert 'id="bundle-detail"' not in page, "the detail is a page, not a card under the list"
    assert 'showPanel("artifact", updateHash, `/${route.id}`)' in script
    assert '["artifact", "storage"].includes(target) ? "artifacts"' in script, "its list stays lit"
    for fact in ("<small>Repository</small>", "<small>Branch</small>", "<small>Started by</small>", "<small>Source</small>"):
        assert fact in script, fact
    assert "function actorLink" in script and "function producerRunLink" in script
    # A run says whose CI supplied its bundle, not "the bundle run X built"
    # about a run that never existed.
    assert "Flashed a bundle supplied by" in script
    # The eight characters every page shows are what gets pasted back.
    assert "bundles start with" in script and "history.replaceState(null, \"\", `#artifact/${matches[0].id}`)" in script


def test_the_bundle_list_is_one_compact_row_each_and_the_row_is_the_way_in():
    """Four buttons on every row made the list a wall of Pin and Delete; they
    are on the bundle's page, and the row links there."""
    page = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")
    assert "<th>By</th>" in page and "Search id, project, branch, revision, user" in page
    rows = script.split("function renderBundles()", 1)[1].split("function appSlot", 1)[0]
    assert 'class="clickable" data-href="${href}"' in rows
    assert "bundle-delete" not in rows and "bundle-pin" not in rows, "actions live on the bundle's page"
    assert 'document.addEventListener("click"' in script and 'event.target.closest("a, button, input, select, label")' in script
    # Its long lists are paged there: the canary is flashed by every check.
    assert "const BUNDLE_PAGE_SIZE = 10" in script and "function pageControls" in script


def test_every_kind_of_storage_has_a_page_of_what_fills_it():
    """The panel said run evidence was 2 GB and nothing else. Each kind links
    to a page of its largest entries, each tied to the run it belongs to, and
    an orphan says so."""
    page = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")
    service = SERVICE.read_text(encoding="utf-8")
    assert 'data-page="storage"' in page
    assert "`#storage/${encodeURIComponent(category.name)}`" in script
    assert "/api/v1/storage/${encodeURIComponent(kind)}?limit=" in script
    assert ">orphan</span>" in script
    assert 'r"/api/v1/storage/([a-z]{1,32})"' in service
    # Nothing on this page makes the Pi walk a directory on a request.
    assert "def storage_detail" in service and "child_usage" in service


def test_every_navigation_goes_through_the_router_whatever_the_browser_fires():
    """`location.hash = ...` only changes the fragment; a page that followed it
    did so because Chromium also fires `popstate` for fragment changes, which
    nothing promises. Navigation makes a history entry and routes directly,
    and a plain `#...` link is routed on `hashchange` as well as `popstate`,
    once per address."""
    script = (WEB / "app.js").read_text(encoding="utf-8")
    assert "location.hash = " not in script, "a bare fragment write depends on the browser"
    assert "function navigateTo" in script and "history.pushState(null, \"\", href)" in script
    assert 'window.addEventListener("hashchange", () => routeFromLocation())' in script
    assert 'window.addEventListener("popstate", () => routeFromLocation())' in script
    assert "if (!force && location.hash === lastRouted) return;" in script, "one route per address"


def test_a_link_opened_before_logging_in_loads_once_the_token_is_given():
    """A shared link to a run, a bundle or a storage page opened in a fresh
    tab routes with no token and skips its requests. Logging in used to reload
    only a plain #artifacts, so the others stayed empty until a reload."""
    script = (WEB / "app.js").read_text(encoding="utf-8")
    login = script.split('$("token-form").addEventListener("submit"', 1)[1].split("});", 1)[0]
    assert "routeFromLocation({force: true})" in login
    assert '=== "#artifacts"' not in login


def test_the_farm_shows_what_it_has_done_not_only_what_it_holds():
    """The overview said how many boards and how long the queue is, and nothing
    about the runs. A Statistics page and a last-seven-days band come from the
    farm's own history; the charts are SVG from attributes and classes, since
    the service's content security policy allows no inline style."""
    page = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'data-panel="statistics"' in page and 'data-page="statistics"' in page
    assert 'id="overview-stats"' in page
    assert 'if (route.name === "statistics") loadStatistics(' in script
    assert "/api/v1/stats?days=${days}&tz_offset_minutes=${tzOffsetMinutes()}" in script
    stats = script.split("// ---- Statistics", 1)[1].split("// ---- Configuration", 1)[0]
    assert " style=" not in stats, "an inline style attribute is dropped by the CSP"
    for tile in ("Pass rate", "Run time", "Queue wait", "Rig busy", "Boards healthy"):
        assert tile in stats, tile
    # History moves slowly: the overview asks at most once a minute.
    assert "if (Date.now() - overviewStatsAt < 60000) return;" in stats


def test_a_rig_page_follows_the_poll_without_closing_what_the_operator_has_open():
    """Every poll re-rendered a rig's page whole: an open settings editor
    closed under the operator and the page jumped up as it shrank."""
    script = (WEB / "app.js").read_text(encoding="utf-8")
    render = script.split("function renderSection(", 1)[1].split("\n}\n", 1)[0]
    assert "renderedHtml.get(element) === html" in render and "details[data-keep]" in render
    rig = script.split("function renderRig()", 1)[1].split("\n}\n", 1)[0]
    assert 'if (rigPage.editing !== "details") renderSection($("rig-summary")' in rig
    assert 'if (rigPage.editing !== "settings") renderSection($("rig-settings")' in rig
    assert ".innerHTML" not in rig, "nothing on a rig's page is rewritten unless it changed"
    # Controls are delegated once, so a section left as it was keeps working.
    assert script.count("installRigPageHandlers();") == 1 and "function installRigHandlers" not in script
    # Settings are grouped, say what they do, and are edited in place.
    for group in ("Runs", "Retention", "Host health"):
        assert f'title: "{group}"' in script, group
    assert "settings-changes" in script and "Set on the rig itself" in script


def test_a_rigs_callmebot_link_is_sealed_in_the_browser_and_never_sent_as_it_is():
    """docs/providers.md, "From the portal": the card everyone sees shows the
    link only redacted; an admin's link is encrypted to the rig's key with
    WebCrypto and only the ciphertext is sent."""
    page = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")
    # The stored link lives with the other things this rig can send through:
    # it was a card of its own that turned out to be this one's details.
    assert 'id="rig-provider"' not in page
    rig = script.split("function renderRig()", 1)[1].split("\n}\n", 1)[0]
    assert '"channel-add", "channel-events", "provider", "provider-policy"' in rig, \
        "a link being typed is not re-rendered away by the poll"
    card = "".join(script.split(f"function {name}(", 1)[1].split("\n}\n", 1)[0]
                   for name in ("callmebotRows", "providerActions", "providerLinkForm", "notifyRows"))
    for shown in ("callmebot.link", "callmebot.send", "used_today", "max_per_day", "provider_callmebot_route",
                  "seal_key", 'type="password"', "isSecureContext", "Seal and send", "providers seal-key"):
        assert shown in card, shown
    assert "isAdmin()" in card, "setting and removing are an admin's"
    seal = script.split("async function sealProviderLink(", 1)[1].split("\n}\n", 1)[0]
    assert 'crypto.subtle.importKey("spki", der, {name: "RSA-OAEP", hash: "SHA-256"}, false, ["encrypt"])' in seal
    assert 'crypto.subtle.digest("SHA-256", der)' in seal, "the fingerprint is of the key actually used"
    submit = script.split('if (form.id === "provider-link-form") {', 1)[1].split('if (form.id === "provider-policy-form")', 1)[0]
    # Read and cleared before anything else happens; sent only sealed.
    assert submit.index('input.value = "";') < submit.index("callmebotLinkProblem(link)") < submit.index("sealProviderLink(")
    assert 'rigCommand(name, "provider_set", {provider: "callmebot", sealed: sealed.sealed, fingerprint: sealed.fingerprint})' in submit
    assert "link" not in submit.split("rigCommand(", 1)[1].split(")", 1)[0].replace("sealed.", "")
    assert "console." not in submit and "localStorage" not in submit and "sessionStorage" not in submit
    assert 'rigCommand(name, "provider_remove", {provider: "callmebot"})' in script and "confirm(`Remove the CallMeBot link" in script
    # The policy is two remote settings through the same configure command.
    assert 'key: "providers.callmebot.send"' in script and 'key: "providers.callmebot.max_per_day"' in script
    assert 'form.id === "provider-policy-form"' in script and 'rigCommand(name, "configure", {settings})' in script
    # No listing of what was asked of a rig shows a sealed value, even the
    # placeholder -- and there are two of them now, the card on the overview
    # and the Activity tab's whole history, so they render a row the same way.
    row = script.split("function commandRow(command)", 1)[1].split("\n}\n", 1)[0]
    assert '["settings", "sealed"]' in row
    for listing in ("function rigActivity(rig)", "async function loadRigHistory(name"):
        assert "commandRow" in script.split(listing, 1)[1].split("\n}\n", 1)[0], listing


def test_an_admin_sends_a_callmebot_test_message_after_confirming_and_sees_how_it_went():
    script = (WEB / "app.js").read_text(encoding="utf-8")
    actions = script.split("function providerActions(rig,", 1)[1].split("\n}\n", 1)[0]
    # Only for an admin, on a rig with a usable stored link, and not while the
    # rig is busy with a provider command or running a job.
    assert "rig.local || !isAdmin()" in actions
    assert "callmebot.link ?" in actions and "Send a real message" in actions
    assert "providerBusy || running" in actions
    assert 'rigPage.busy.has("provider_test")' in actions
    # The last test the rig was asked for, and what it said, in its details.
    rows = script.split("function callmebotRows(rig)", 1)[1].split("\n}\n", 1)[0]
    assert '(rig.commands || []).find(command => command.kind === "provider_test")' in rows
    assert '"Last real message"' in rows and "lastTest.detail" in rows
    handler = script.split('if (has("provider-test")) {', 1)[1].split("\n    }\n", 1)[0]
    asked = handler.index("confirm(`Send one WhatsApp to the number stored on ${name}? It counts against today's budget (")
    assert asked < handler.index('rigCommand(name, "provider_test", {provider: "callmebot"})')
    assert "used_today" in handler and "max_per_day" in handler and ")) return;" in handler
    assert 'provider_test: "Send CallMeBot test"' in script


def test_firmware_is_a_library_by_project_and_branch_not_a_growing_list():
    """A list of every bundle grows with every CI run. The page leads with
    each project's branches -- the latest build, the last run on it, what is
    older -- and keeps the whole list and the disk behind tabs."""
    page = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")
    firmware = page.split('data-page="artifacts"', 1)[1].split('data-page="artifact"', 1)[0]
    assert 'id="firmware-tabs"' in firmware and 'href="#artifacts/all"' in firmware and 'href="#artifacts/storage"' in firmware
    assert firmware.index('id="firmware-library"') < firmware.index('id="firmware-all"') < firmware.index('id="firmware-storage"')
    storage = firmware.split('id="firmware-storage"', 1)[1]
    assert 'id="storage"' in storage and 'id="retention"' in storage and 'id="prune-form"' in storage
    assert 'api("/api/v1/artifacts/library")' in script
    row = script.split("function libraryRow(", 1)[1].split("\n}\n", 1)[0]
    for part in ("group.latest", "group.last_run", "group.older_bytes", "library-run", "library-all"):
        assert part in row, part
    # "N builds" is the whole list, filtered to that branch, which can be let go.
    assert 'params.set("branch", bundleQuery.branch)' in script and 'id="bundle-branch"' in page
    # One scan of the store a visit, and the figures follow a prune.
    load = script.split("async function loadArtifacts(", 1)[1].split("\n}\n", 1)[0]
    assert 'if (firmwareTab === "library") await loadLibrary();' in load and "else await loadBundleList();" in load
    assert "loadLibrary();\n" not in load.split('if (firmwareTab === "library")', 1)[0]
    follow = script.split("async function followPrune(", 1)[1].split("\n}\n", 1)[0]
    assert "await refreshLibraryAfterPrune();" in follow and "renderFirmwareMetrics(index);" in follow
    assert "lastLibrary.pruning?.active" in script
    # A build is run from the Runs page, where the run form is now.
    run = script.split("function runWithBundle(", 1)[1].split("\n}\n", 1)[0]
    assert 'navigateTo("#runs")' in run and '$("run-card").open = true' in run


def test_settings_say_what_each_decision_is_and_where_it_is_changed():
    page = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")
    settings = page.split('data-page="configuration"', 1)[1]
    assert 'id="settings-tabs"' in settings and 'href="#configuration/projects"' in settings and 'href="#configuration/access"' in settings
    assert 'id="config-projects"' in settings and 'id="keys"' in settings.split('id="settings-access"', 1)[1]
    render = script.split("function renderConfig(config)", 1)[1].split("\n}\n", 1)[0]
    for group in ("Quarantine", "Retention", "Notifications", "Backup"):
        assert f'viewGroup("{group}"' in render, group
    # A portal says which file it read its settings from; it does not name
    # the deployment's ConfigMap, which is ours and ships to a rig
    # (tests/test_public_scrub.py).
    assert "config_file" in render and "read on each decision" in render
    assert "alteriom-hil-admin config set" in render


def test_every_section_of_a_rigs_page_belongs_to_exactly_one_tab():
    """A tab that only hid sections after a render lost every race with a
    renderer that finished later -- the runs arriving, a command's result
    coming back -- and the boards tab then carried the overview's header, its
    host card and its activity. Which tab a section is on is written on the
    section, and applied in the stylesheet, where nothing can undo it."""
    page = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")
    style = css_rules((WEB / "app.css").read_text(encoding="utf-8"))

    rig_page = page.split('data-page="rig"', 1)[1].split("data-page=", 1)[0]
    sections = re.findall(r'<(?:section|article) id="(rig-[a-z-]+)"([^>]*)>', rig_page)
    tabs = [tab for tab in re.findall(r'\{id: "([a-z]+)", label:', script)]
    assert tabs == ["overview", "boards", "runs", "activity", "setup"]
    for name, attributes in sections:
        if name in ("rig-health", "rig-activity"):
            continue  # inside rig-grid, which carries the tab for both
        found = re.search(r'data-tab="([a-z ]+)"', attributes)
        assert found, f"{name} is on every tab, which is what the tabs were for"
        assert set(found.group(1).split()) <= set(tabs), name
    # Each tab shows its own and hides the rest, and a section that hid itself
    # stays hidden: the rule only ever hides.
    for tab in tabs:
        assert f'.page[data-rig-tab="{tab}"] > [data-tab~="{tab}"]:not([hidden])' in style, tab
    assert ".page[data-rig-tab] > [data-tab]{display:none}" in style
    assert ".page[data-rig-tab] > [data-tab][hidden]{display:none}" in style
    # `hidden` decides on its own rather than by out-weighing the rules that
    # show: the grid's rule names an id, which beat the rule written to keep a
    # hidden section hidden, and a pending rig then carried the last rig's
    # health and activity under its join command. Every rule that shows one of
    # these sections says :not([hidden]), whatever it is weighed against.
    for rule in re.findall(r"\.page\[data-rig-tab[^{]*\{display:(?!none)[^}]*\}", style):
        for selector in rule.split("{", 1)[0].split(","):
            assert ":not([hidden])" in selector, selector.strip()
    # And the tab bar is the only thing that changes tab: every section now
    # carries data-tab, so a click inside one must not be read as a tab click.
    assert 'target.closest?.("#rig-tabs .tab")' in script


def test_a_rigs_channels_list_only_what_is_set_up_and_who_is_told_what():
    """The page offered a CallMeBot setup box on every rig, whether or not one
    was set up, and said "nothing is set up" for a rig that had simply never
    reported its configuration. Now each channel that exists is a card of its
    own, with its own buttons, and the two silences are told apart."""
    script = (WEB / "app.js").read_text(encoding="utf-8")
    card = script.split("function rigChannels(rig)", 1)[1].split("\nfunction ", 1)[0]
    # What is set up, per channel -- including the rig's own link, which is
    # the credential CallMeBot notifies through.
    assert 'kind === "callmebot"' in card and "callmebot.url_file" in card
    # And the picker offers CallMeBot only over a link that can be used: the
    # path alone survives a removal, and choosing it would turn notifications
    # on against a file that is not there.
    picker = script.split("function channelForm(rig", 1)[1].split("\n}\n", 1)[0]
    assert 'value !== "callmebot" || rig.config?.callmebot?.link' in picker
    # And the remembered choice is normalised against what this rig offers:
    # kept from a rig that had a link, the form was built from CallMeBot's
    # spec while the browser selected Telegram, and submitting read a field
    # that was never rendered.
    assert "offered.some(([value]) => value === rigPage.channelKind)" in picker
    assert "rigPage.channelKind = kind" in picker
    # A seal key and a secure context are what a credential is sealed with,
    # and CallMeBot has no credential to seal: a rig set up on the rig itself
    # has a link and no key, and a dashboard served over plain http can still
    # point it at one. Both were refused before the picker was even built.
    assert "item.optionalSecret || sealable" in picker
    assert "const canAdd = Boolean(sealKey) || callmebotLink" in card
    # And the form says what it is doing: nothing is sealed for that channel.
    assert 'spec.optionalSecret ? "Use this channel" : "Seal and send"' in picker
    assert "Nothing is sent but the choice" in picker
    # A rig that has told the portal nothing is not a rig with nothing set up.
    assert "has not reported its configuration to the portal yet" in card
    assert "Nothing is set up" in card
    # Each card carries its own actions rather than one row at the top, and
    # the same button turns it off and back on.
    assert "channel-test" in card and "channel-events" in card
    # A channel that is off keeps its credential, and `alteriom-hil-admin
    # notify test` sends down it anyway, reporting that nothing else will be
    # until it is on. Trying it is how an operator decides whether to turn it
    # back on, so the test is refused only while the rig is busy or running.
    assert 'class="secondary channel-test" data-id="${escapeHtml(entry.id)}"${busy || running ? " disabled" : ""}' in card
    cli = (Path(__file__).resolve().parents[1] / "rig" / "alteriom_hil" / "admin_cli.py").read_text(encoding="utf-8")
    sends = cli.split("def command_notify_test(", 1)[1].split("\ndef ", 1)[0]
    assert "notify.enabled is false" in sends and "return 1" in sends, (
        "if the CLI stops sending down a channel that is off, the button should stop offering it"
    )
    assert 'channel-${entry.enabled ? "off" : "on"}' in card
    for handler in ('has("channel-off")', 'has("channel-on")', 'has("channel-events")'):
        assert handler in script, handler
    # There is no provider card: what it showed is a channel's details, and
    # setting one up is offered from the channels themselves.
    assert "function rigProvider(" not in script
    assert "provider-set-open" in card, "and a rig with no link is offered one here"
    assert "channel-details" in card and "Hide details" in card
    # Which events a rig sends is a setting, so changing it never asks for the
    # credential again.
    assert 'rigCommand(name, "notify_tune"' in script
    assert "enabled: true" in script and "enabled: false" in script
    for event in ("queue_paused", "board_red", "host_unhealthy"):
        assert event in script, event


def test_a_rigs_runs_and_activity_are_read_back_a_page_at_a_time():
    """Both lists were the last ten with no way further back."""
    script = (WEB / "app.js").read_text(encoding="utf-8")
    runs = script.split("async function loadRigRuns(name", 1)[1].split("\n}\n", 1)[0]
    assert "offset=${Math.max(0, offset)}" in runs and "runs-page" in runs
    assert "Newer" in runs and "Older" in runs
    history = script.split("async function loadRigHistory(name", 1)[1].split("\n}\n", 1)[0]
    assert "commands?limit=${limit}&offset=" in history and "history-page" in history
    # The runs tab carries the controls for what to start on this rig.
    # The controls are their own section, rendered with the rig rather than
    # with the list of runs: the runs card is replaced only when the runs are
    # fetched, so a button inside it never took the disabled state a command
    # in progress gives it, and could be clicked again and again.
    assert "function runControls(rig)" in script
    assert 'renderSection($("rig-run-controls"), runControls(rig))' in script
    page = (WEB / "index.html").read_text(encoding="utf-8")
    assert '<section id="rig-run-controls" data-tab="runs"></section>' in page
    runs = script.split("async function loadRigRuns(name", 1)[1].split("\n}\n", 1)[0]
    assert "runControls" not in runs, "the list of runs does not own the controls"
    # The activity card links to the whole history rather than growing.
    assert 'rig-tab-go" data-go="activity"' in script
    assert 'if (id === "activity" && rigPage.name) loadRigHistory(rigPage.name, 0)' in script


def test_a_tab_that_is_kept_across_rigs_does_not_keep_the_last_rigs_content():
    """The tab is kept when an operator follows a link from one rig to
    another -- reading activity across rigs stays in activity -- but what it
    holds is the rig that was opened, not the one that was left. A pending rig
    has no tab bar at all, so a tab held from the last rig would filter its
    join command away with no control to bring it back."""
    script = (WEB / "app.js").read_text(encoding="utf-8")
    show = script.split("async function showRig(name)", 1)[1].split("\n}\n", 1)[0]
    # Arriving at another rig empties the paged lists rather than showing the
    # last rig's rows under this rig's name.
    # Every section the page fetches for itself is emptied, not only the one
    # whose tab is open: the runs card carries controls that act on whichever
    # rig is open, so the last rig's rows under this rig's buttons was a click
    # on the wrong rig waiting to happen.
    assert "runsOffset: 0, historyOffset: 0" in show
    assert '["rig-runs", "rig-history"].forEach' in show and "forgetRendered($(id))" in show
    assert '$("rig-logs").hidden = true' in show
    assert 'if (rigPage.tab === "activity") loadRigHistory(name, arrived ? 0 :' in show
    # And the poll keeps the page the operator is reading.
    assert "loadRigRuns(name, rigPage.runsOffset || 0)" in show
    rig = script.split("function renderRig()", 1)[1].split("\n}\n", 1)[0]
    pending = rig.split("if (pending) {", 1)[1]
    assert 'rigPage.tab = "overview"' in pending and "applyRigTab()" in pending


def test_every_editor_of_a_channel_opens_in_the_card_it_belongs_to():
    """Budget, Store a link and the rest rendered into a separate section
    further down the page -- one that was hidden when there was no link, so
    the button appeared to do nothing at all. Each opens where it was clicked."""
    script = (WEB / "app.js").read_text(encoding="utf-8")
    opening = script.split('has("provider-set-open") || has("provider-policy-open")', 1)[1].split("return;", 1)[0]
    assert 'renderSection($("rig-channels"), rigChannels(rig))' in opening
    assert "rig-provider" not in script, "there is no second section to render into"
    # And the card that was clicked opens its own details, one at a time.
    details = script.split('has("channel-details")', 1)[1].split("return;", 1)[0]
    assert "rigPage.channelOpen === key ? null : key" in details
    card = script.split("function rigChannels(rig)", 1)[1].split("\nfunction ", 1)[0]
    for form in ("channel-add", "channel-events", "provider", "provider-policy"):
        assert f'editing === "{form}"' in card, form


def test_a_rig_that_cannot_be_opened_says_so_whatever_tab_was_held():
    """The failure is rendered into the summary, which belongs to Overview.
    Held on Boards or Activity, an operator opening a deleted rig was shown an
    empty page -- or Activity's "Loading..." -- with no tab bar to get back,
    because the bar is only rendered for a rig that answered."""
    script = (WEB / "app.js").read_text(encoding="utf-8")
    show = script.split("async function showRig(name)", 1)[1].split("\n}\n", 1)[0]
    # From the failure being rendered to the end of the catch.
    failure = show.split("failure-summary", 1)[1].split("return;", 1)[0]
    assert 'rigPage.tab = "overview"' in failure and "applyRigTab()" in failure
    assert '$("rig-tabs").hidden = true' in failure
    # Every section but the one carrying the failure, taken from the page
    # rather than from a list here: listed by hand it went stale as soon as a
    # section was added, and a rig that could not be opened showed the last
    # rig's setup card under the error.
    assert "rigSections().forEach" in failure and 'node.id !== "rig-summary"' in failure
    assert "function rigSections()" in script
    assert '.page[data-page="rig"] > [data-tab]' in script


def test_a_callmebot_link_that_was_removed_is_not_a_channel_that_is_on():
    """Removing a link deletes the credential file and leaves its path in the
    configuration, so the path says nothing about whether anything can be
    sent. The rig reports the usable link as `callmebot.link`; a card that
    claimed a working provider over a file that is no longer there was
    reporting the opposite of the failure."""
    script = (WEB / "app.js").read_text(encoding="utf-8")
    card = script.split("function rigChannels(rig)", 1)[1].split("\nfunction ", 1)[0]
    assert "const callmebotLink = Boolean(callmebot.link)" in card
    assert "? callmebotLink" in card, "whether the CallMeBot channel is set up is the link, not its path"
    assert "no usable link" in card, "a link that has gone says so rather than showing a send policy"
    actions = script.split("function providerActions(rig,", 1)[1].split("\n}\n", 1)[0]
    assert 'callmebot.link ? "Replace link" : "Store a link"' in actions
    assert "callmebot.link ?" in actions, "nothing is offered over a link that cannot be used"


def test_a_control_in_the_page_heading_opens_the_tab_it_renders_into():
    """The heading is on every tab, and two of its buttons render into
    sections that are not: Logs writes into Setup's card and Edit into
    Overview's summary. Without taking the operator there, Logs scrolled to
    something the tab was hiding and Edit opened an editor nobody could see."""
    script = (WEB / "app.js").read_text(encoding="utf-8")
    logs = script.split('if (kind === "logs" && outcome?.status === "done")', 1)[1].split("} else", 1)[0]
    assert 'goToRigTab("setup")' in logs and logs.index('goToRigTab("setup")') < logs.index("bringIntoView")
    edit = script.split('if (has("rig-edit")) {', 1)[1].split("return;", 1)[0]
    assert 'rigPage.tab = "overview"' in edit and "applyRigTab()" in edit

def test_an_answer_for_the_rig_that_was_left_never_lands_on_the_one_that_is_open():
    """Both paged lists guarded the answer that arrived and not the way a
    request fails, so switching rigs while a request was in flight could put
    rig A's error over rig B's page."""
    script = (WEB / "app.js").read_text(encoding="utf-8")
    for loader, counter in (("async function loadRigHistory(name", "historyRequest"),
                            ("async function loadRigRuns(name", "runsRequest")):
        body = script.split(loader, 1)[1].split("\n}\n", 1)[0]
        # Two requests for the same rig can be in flight at once -- the
        # operator clicks Older while the poll asks for the page it already
        # knows about -- and the answer that arrives last is not the one that
        # was asked for last. Each answer says which request it belongs to.
        assert f"rigPage.{counter} = (rigPage.{counter} || 0) + 1" in body, loader
        assert f"rigPage.{counter} === asked" in body, loader
        assert "rigPage.name === name" in body, loader
        # On the way a request fails as much as on the way it succeeds.
        failure = body.split("catch (error)", 1)[1].split("}", 1)[0]
        assert "if (!current()) return" in failure, loader


def test_every_configure_reloads_the_rig_it_changed():
    """`rigCommand` reloads a rig's page after a command, except a `configure`
    -- that one is left to the caller, because it has an editor to close
    first. A caller that forgot left the card it had just changed showing the
    state it was rendered in: turned on and still saying off, with its buttons
    disabled, until some later poll happened to correct it.

    So every `configure` in the page is checked for the reload that follows
    it, rather than the four that exist today being listed here.
    """
    script = (WEB / "app.js").read_text(encoding="utf-8")
    skips = script.split("rigPage.busy.delete(kind)", 1)[1].split("\n}\n", 1)[0]
    assert 'kind !== "configure"' in skips, "if rigCommand reloads it itself, this test is moot"

    sites = [index for index in range(len(script))
             if script.startswith('rigCommand(name, "configure"', index)]
    assert len(sites) >= 2, sites
    for index in sites:
        after = script[index:index + 900]
        assert "showRig(name)" in after or "finishChannelChange(name" in after, (
            f"a configure with no reload after it: ...{script[index:index + 90]}"
        )


def test_a_change_a_rig_has_not_reported_yet_is_not_shown_as_its_state():
    """A rig reports a command's result first and its configuration on the
    next heartbeat (farm_node._report_control_results), so for a few seconds
    the portal holds the configuration from before the change. Reloading the
    card then rendered what the rig had already stopped doing -- "off" under a
    channel that had just been turned on -- and offered buttons over it."""
    script = (WEB / "app.js").read_text(encoding="utf-8")
    finish = script.split("function finishChannelChange(name, outcome)", 1)[1].split("\n}\n", 1)[0]
    assert 'outcome?.status === "done"' in finish and "rigPage.channelPending" in finish
    # It stops being pending when the rig reports something newer, not after a
    # guessed wait.
    decide = script.split("function channelChangePending(rig)", 1)[1].split("\n}\n", 1)[0]
    assert "Date.parse(rig.config_at" in decide and "rigPage.channelPending = null" in decide
    # Whose change, not just when: a bare stamp was compared against whatever
    # rig was opened next, and that rig -- reporting on its own schedule --
    # looked like the one with something unreported, its buttons withheld.
    assert 'rigPage.channelPending.name !== rig.name' in decide
    assert "{name, at: outcome.finished_at" in finish
    card = script.split("function rigChannels(rig)", 1)[1].split("\nfunction ", 1)[0]
    assert "const pending = channelChangePending(rig)" in card
    assert "has applied the change and not yet reported" in card
    assert "manageable && !pending" in card, "nothing is offered over a state the rig has left"


def test_the_page_a_pager_asked_for_is_recorded_before_it_is_asked_for():
    """A poll can already be awaiting the rig's detail when the operator
    clicks Older. Taken from the answer rather than the asking, the offset the
    poll resumes with is the page that was clicked away from -- a newer
    request for an older page, which supersedes the click."""
    script = (WEB / "app.js").read_text(encoding="utf-8")
    for loader, field in (("async function loadRigHistory(name", "historyOffset"),
                          ("async function loadRigRuns(name", "runsOffset")):
        body = script.split(loader, 1)[1].split("\n}\n", 1)[0]
        before, after = body.split("await api(", 1)
        assert f"rigPage.{field} = Math.max(0, offset)" in before, loader
        assert f"rigPage.{field} = page.offset" in after, f"{loader}: the server still has the last word"



def test_a_rig_has_channels_and_no_separate_idea_of_a_provider():
    """There were two ideas on the page: a "channel" the rig reported problems
    on, and a "provider" a board sent through during a run -- whose card,
    further down, was the details and the editor of a channel listed above it.
    A Budget button therefore appeared to do nothing: it opened a section
    somewhere else. They are one thing seen from two sides."""
    page = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'id="rig-provider"' not in page and "function rigProvider(" not in script
    card = script.split("function rigChannels(rig)", 1)[1].split("\nfunction ", 1)[0]
    # Each card says which side it is: what tells you, and what a board sends
    # through during a run.
    assert "the rig tells you when something breaks" in card
    assert "a board sends through it during a run" in card
    # A CallMeBot channel is the stored link seen from the notifying side, so
    # it carries the link's buttons rather than being listed twice.
    assert 'kind === "callmebot" ? providerActions(' in card
    # Listed separately only when it is not the notifying channel -- or when
    # it is, and its link can no longer be used, because then that card is the
    # only place left to repair it.
    assert "callmebot.url_file && (!notifiesThroughLink || !callmebotLink)" in card
    # The details of either are the rows the provider card used to show.
    assert "callmebotRows(rig)" in card and "notifyRows(rig," in card


def test_a_link_that_cannot_be_used_can_always_be_repaired_from_the_page():
    """Remove link on a rig that notifies through CallMeBot left `url_file`
    set and `link` null. The notify card then needed a usable link to appear,
    the separate card was only for a rig that notified some other way, and the
    heading's offer was suppressed by the path that was still there: a state
    reachable by one click on the page, with nothing on the page to undo it."""
    script = (WEB / "app.js").read_text(encoding="utf-8")
    card = script.split("function rigChannels(rig)", 1)[1].split("\nfunction ", 1)[0]
    # Offered whenever there is no usable link, whatever the path says.
    assert 'callmebotLink || !sealKey ? "" :' in card
    assert 'callmebot.url_file ? "Replace CallMeBot link" : "Add CallMeBot link"' in card
    # And the link keeps a card of its own while it is unusable, even when it
    # is what the rig notifies through -- that card is where it is repaired.
    assert "callmebot.url_file && (!notifiesThroughLink || !callmebotLink)" in card


def test_a_rig_is_offered_another_notification_rather_than_a_replacement():
    """It could hold one channel, so the button said Change. A rig can be told
    to say things in several ways now -- and however many places its events
    go -- so the button offers to add one."""
    script = (WEB / "app.js").read_text(encoding="utf-8")
    card = script.split("function rigChannels(rig)", 1)[1].split("\nfunction ", 1)[0]
    assert '<button class="secondary channel-add">Add a notification</button>' in card
    assert "Change channel" not in card


def test_what_a_rig_can_do_is_under_its_name_on_every_tab_and_in_the_list():
    """"Does this rig have a broker" is the first question asked of a rig, and
    the answer was a card two tabs away. The chips are in the page heading
    now, and under the rig's name in the rigs list, from one renderer: what
    is on and what is broken, in the rig's own words (the row's `label`),
    never what is off -- a heading says what a rig can do, not what it
    cannot."""
    page = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")
    style = css_rules((WEB / "app.css").read_text(encoding="utf-8"))
    assert '<div id="rig-capabilities" class="setup-chips heading-chips"></div>' in page
    # The renderer is chips.js, loaded before app.js: the world page loads
    # the same file, so a chip means the same thing on both.
    shared = (WEB / "chips.js").read_text(encoding="utf-8")
    assert page.index('<script src="/chips.js">') < page.index('<script src="/app.js">')
    assert "function capabilityChips" not in script and "function escapeHtml" not in script
    chips = shared.split("function capabilityChips(rows, {clickable = false} = {})", 1)[1].split("\n}\n", 1)[0]
    assert '["on", "broken"].includes(row.state)' in chips, "off and unknown are not chips"
    assert "escapeHtml(row.label || row.title)" in chips, "the rig's words: 8 boards, Wi-Fi AP Â· ch 1"
    assert 'class="setup-chip ${state.tone} rig-capability" data-key=' in chips, "in the heading, a chip is a button"
    # Rendered with the heading, from the same rig, and never for a rig that
    # has not joined; cleared with the rest when a rig cannot be opened.
    render = script.split("function renderRig()", 1)[1].split("\nfunction ", 1)[0]
    assert 'renderSection($("rig-capabilities"), pending ? "" : capabilityChips(rig.setup, {clickable: true}))' in render
    assert script.count('renderSection($("rig-capabilities"), "")') == 1
    # A chip is a way in: the Setup tab, its table open, at that capability's row.
    click = script.split('const capability = target.closest?.(".rig-capability");', 1)[1].split("return;", 1)[0]
    assert "rigPage.setupOpen = true" in click and 'goToRigTab("setup")' in click
    assert "const row = $(`setup-row-${capability.dataset.key}`)" in click and "if (row) bringIntoView(row)" in click
    assert '<tr id="setup-row-${escapeHtml(row.key)}">' in script
    # The rigs list carries the same chips under the name, from the same
    # renderer, off the list view's own `setup` (rig_setup.headline).
    row = script.split("function rigRow(rig)", 1)[1].split("\nfunction ", 1)[0]
    assert "const chips = capabilityChips(rig.setup)" in row
    assert '<div class="setup-chips compact">${chips}</div>' in row
    # And the setup card no longer says the heading's chips a second time.
    card = script.split("function rigSetup(rig)", 1)[1].split("\nfunction ", 1)[0]
    assert "setup-chips" not in card
    # A chip that is a button does not look like the accent-coloured buttons
    # around it: the class resets what `button` paints.
    assert "button.setup-chip{cursor:pointer}" in style
    assert ".heading-chips:empty{display:none}" in style
    assert ".setup-chips.compact .setup-chip{" in style


def test_the_login_card_offers_github_and_email_and_keeps_the_key_for_programs():
    """A person signs in with GitHub or by email and has a session cookie the
    page never sees; a key pasted into the tab still works, for a program
    or an operator with one. The page asks the farm who it is before it
    shows the card, and sends the key only when it has one."""
    page = (WEB / "index.html").read_text(encoding="utf-8")
    script = (WEB / "app.js").read_text(encoding="utf-8")
    assert '<a id="signin-github" class="button signin-github" href="/auth/github" hidden>' in page
    assert '<form id="signin-email-form" hidden>' in page and 'id="signin-email" type="email"' in page
    # Two steps in one card: ask for a code, then type it here. The code
    # field is a one-time-code field so a phone offers it from the mail,
    # and the second step posts JSON with the browser's own cookies -- the
    # nonce the first step set is what makes it the browser that asked.
    assert '<form id="signin-code-form" hidden>' in page and 'autocomplete="one-time-code"' in page
    assert "Send code" in page and "link that signs you in" not in page
    assert 'fetch("/auth/email/code", {method: "POST", credentials: "same-origin"' in script
    assert 'location.href = body.next || "/app"' in script, "and goes where the farm says"
    assert '<details id="signin-key">' in page and 'id="token-form"' in page
    assert '<button id="sign-out" class="secondary sign-out" hidden>Sign out</button>' in page
    call = script.split("async function api(path, options = {})", 1)[1].split("\n}\n", 1)[0]
    assert 'token && token !== "session"' in call, "a session sends no Authorization header; the cookie goes with the request"
    files = script.split("async function fetchFromApi(path)", 1)[1].split("\n}\n", 1)[0]
    assert 'token && token !== "session"' in files and 'credentials: "same-origin"' in files, \
        "a file -- an image, an archive, a log -- is fetched the same way, or a signed-in person cannot open one"
    assert 'headers: {"Authorization": `Bearer ${token}`}}' not in files
    boot = script.split("async function bootSession()", 1)[1].split("\n}\n", 1)[0]
    assert 'fetch("/api/v1/whoami", {credentials: "same-origin"})' in boot
    assert 'token = "session"' in boot and "loadSignInOptions()" in boot
    # A guest -- signed in, not let in -- is told so on the card, with the
    # ways in hidden and sign-out offered, rather than shown a dashboard
    # whose every call is refused.
    assert 'if (who.role === "guest") {' in boot and "has not opened your account yet" in boot
    assert '$("signin-methods").hidden = true' in boot and '$("sign-out").hidden = false' in boot
    assert boot.index('if (who.role === "guest")') < boot.index('token = "session"'), "a guest never becomes a session for the poll"
    # A key this tab kept and the farm no longer takes is let go, and the
    # ways in are offered -- not a card with only "paste another key" on it.
    assert 'if (token && token !== "session") {' in boot
    assert 'headers: {"Authorization": `Bearer ${token}`}' in boot
    assert 'sessionStorage.removeItem("farmToken")' in boot and "no longer accepted" in boot
    assert "if (token) return refresh(true);" not in boot
    assert "if (token) refresh(true);" not in script and "bootSession();" in script
    options = script.split("async function loadSignInOptions()", 1)[1].split("\n}\n", 1)[0]
    assert 'fetch("/auth/options")' in options and '$("signin-key").open = !(options.github || options.email)' in options
    assert '"/auth/github?next="' in options, "a person comes back to the page they were on"
    assert 'fetch("/auth/email", {method: "POST", credentials: "same-origin",' in script
    assert 'fetch("/auth/signout", {method: "POST"' in script


def test_a_session_the_farm_has_ended_brings_the_card_back():
    """A session ends on the farm's side -- thirty days on, or signed out
    from another tab -- and the tab that had it is told 401. Left as it
    was, the tab kept the dashboard it had, kept polling under a sentinel
    nothing would clear, and offered no way back in short of a reload. The
    sentinel is dropped, the poll stops, and the card comes back with the
    ways in."""
    script = (WEB / "app.js").read_text(encoding="utf-8")
    call = script.split("async function api(path, options = {})", 1)[1].split("\n}\n", 1)[0]
    files = script.split("async function fetchFromApi(path)", 1)[1].split("\n}\n", 1)[0]
    assert "if (response.status === 401) sessionEnded();" in call
    assert "if (response.status === 401) sessionEnded();" in files
    ended = script.split("function sessionEnded()", 1)[1].split("\n}\n", 1)[0]
    assert 'if (token !== "session") return;' in ended, "a pasted key that is refused is another story, told elsewhere"
    assert 'token = "";' in ended and "clearTimeout(pollTimer)" in ended
    assert '$("login").hidden = false' in ended and '$("dashboard").hidden = true' in ended
    assert '$("signin-methods").hidden = false' in ended and "loadSignInOptions()" in ended
    assert "session has ended" in ended


def test_settings_projects_is_a_persons_workspaces_and_the_farms_build_for_a_rig():
    """Settings -> Projects showed the farm's build configuration to
    everybody -- which an account is handed as {} -- while a person's own
    projects were only on the Workspaces tab (docs/public-release-plan.md,
    phase 4: workspaces and projects). A signed-in person now sees their
    workspaces and the projects in each, drawn by the rig's bundle from
    whatever the shell answers; a rig, or a key, sees what this farm runs."""
    script = dashboard()
    assert 'if (settingsTab === "general") loadConfig();' in script
    assert 'if (settingsTab === "projects") loadProjects();' in script
    loader = script.split("async function loadProjects()", 1)[1].split("\n}", 1)[0]
    assert "if (!(you?.account && shell().workspaceProjects)) return loadConfig();" in loader
    assert "renderWorkspaceProjects(page.workspaces || [])" in loader
    # A rig has no people: it says so and shows what it runs.
    rig_shell = script.split("const RIG_SHELL", 1)[1].split("};", 1)[0]
    assert "workspaceProjects: null" in rig_shell
    assert "function renderFarmProjects(build)" in script and "What the farm runs" in script
    # The first route can run before the status knows who is looking.
    assert 'if (settingsTab === "projects" && projectsShownAs !== (you?.name || "")) loadProjects();' in script
    # Whom the panel was drawn for is remembered on every path, including the
    # one where the farm's view could not be read (an account is refused it).
    # Not drawn before the first status says who is looking: no flash of the
    # farm's view for a person, no wasted read for anybody.
    assert 'if (!document.body.dataset.mode) return;' in loader
    assert 'projectsShownAs = you?.name || "";' in loader
    # Whoever administers the platform keeps sight of what the farm runs,
    # below their own projects.
    assert "if (isAdmin()) {" in loader and 'api("/api/v1/config")' in loader and "farmProjectsMarkup(config.build || {})" in loader
    # The shell's tabs are built once the shell is known, not only on the
    # first route, which a rig's shell answered with none.
    status = script.split("if (settingsWanted) showSettingsTab(settingsWanted);", 1)[1][:400]
    assert "buildSettingsTabs();" in status
    panel = script.split("function renderWorkspaceProjects(workspaces)", 1)[1].split("\n}", 1)[0]
    assert "No project yet" in panel and "no workspace yet" in panel
    assert 'href="#configuration/workspaces"' in panel


def test_settings_is_a_persons_page_too_and_only_the_farm_wide_tabs_are_an_operators():
    """The Settings page and its nav item were `farm-wide` from when everything
    under them was the farm's, so a signed-in person had no Settings at all --
    and with it no Workspaces, no Projects, no visibility. Found opening
    #configuration/projects as a plain user and landing on the overview. The
    page is everybody's now; General (the farm's configuration) and Access
    (its keys and audit) carry the mark themselves, and a person's first tab
    is the first of their own."""
    page = (WEB / "index.html").read_text(encoding="utf-8")
    script = dashboard()
    assert '<button class="nav-item" data-panel="configuration">Settings</button>' in page
    assert '<section class="page" data-page="configuration" hidden>' in page
    assert '<a href="#configuration" data-tab="general" class="active farm-wide">General</a>' in page
    assert 'data-tab="access" class="admin-only"' in page
    tabs = script.split("function settingsTabs()", 1)[1].split("\n}", 1)[0]
    assert 'workspaceOnly() ? tabs.filter(name => name !== "general" && name !== "access") : tabs' in tabs
    assert "settingsTab = known ? tab : settingsTabs()[0];" in script
    assert 'settingsTab = known ? tab : "general"' not in script
