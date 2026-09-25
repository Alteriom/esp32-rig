let token = sessionStorage.getItem("farmToken") || "";
// Who the key is -- {name, role} -- from the status the service answers with.
let you = null;
// What the rig last said about its GitHub token (the view, or the card), for the attention list.
let lastGithubSummary = null;
// What the rig last said about updates (GET /api/v1/update), for the card and the attention list.
let lastUpdateView = null;
let updateLoadedAt = 0;
let updateTimer = null;
let selectedJobId = null;
let selectedJob = null;
let pollTimer = null;
let pollInFlight = false;
let statusSignature = "";
let detailSignature = "";
let hasActiveJob = false;
let rigBusy = false;
// A running job that has the rig to itself (the rig lock exclusively): no
// serial port may be opened beside it. Jobs sharing the rig hold only their
// own boards, so a free board can still be read.
let rigAlone = false;
let familySignature = "";
let lastInventory = {};
let lastQueue = {paused: false, queued: []};
let repositories = {farm: null, profiles: {}};
// name -> {label, default_ref, repo, suite_path, ...} as reported by the
// farm service. The run form is built from this, so adding a consumer stays
// a data change rather than an edit here.
let profiles = {};
// The profile a run is for when it names none, and the one the run form
// opens on: the farm says (status.default_profile). The dashboard knows no
// project by name.
let defaultProfile = null;
let durationTimer = null;
// The farm's shape (docs/portal-plan.md). standalone: this service has the
// boards. portal: it has none -- nodes connect to it, run what it leases them
// and install the release it names. node: a host whose runs come from a
// portal; its own page shows the boards, not a place to start runs.
let farmMode = "standalone";
let workers = [];
let currentRelease = null;
let portalUrl = null;
const UPDATING = ["pending", "downloading", "staged", "installing"];
// Which application this is, said by the document itself: the portal's page
// declares data-app="portal", the rig's declares "rig". Known at parse time,
// so nothing has to wait for the first status to find out which shell it
// runs in -- which is where a whole family of first-route bugs came from.
function isPortal() { return document.body.dataset.app === "portal"; }

// ---- The shell ---------------------------------------------------------------
// The rig's pages -- a rig, its boards, its runs, its health -- are the same
// on a rig and on the portal that rig connects to. What differs is the shell
// around them: whether there is a fleet or one host, a release to follow,
// rigs to add, webhooks to manage. Every such decision is a named member of
// the shell, and nothing else asks which mode this is. The cut between the
// rig's bundle and the portal's shell follows this seam
// (docs/public-release-plan.md, step 11).
const RIG_SHELL = {
  name: "rig",
  // The noun for what is being looked at, wherever the page speaks of it.
  site: "this rig",
  Site: "This rig",
  // The rig's own key is the rig, not "farm" (the token's name in the
  // service); whoever holds it administers this rig and nothing wider.
  keyLabel: name => name === "farm" ? "this rig" : name,
  keyTitle: role => role === "admin"
    ? "The rig's own key: everything this rig can do"
    : "A key of this rig: runs, health checks and bundles; not the queue, cleanup or the board registry",
  overviewRigs: {eyebrow: "THIS RIG", title: "This rig", action: '<a class="button secondary" href="#rigs">Boards</a>'},
  localRigLabel: "This rig",
  // Settings -> Projects, for a key: on a rig the projects are its own to keep.
  keyProjects: () => loadRigProjects(),
  projectsAreOwn: true,
  // Overview is this rig's own page: the same sections the portal shows for
  // a rig, from the same renderer. The fleet overview is the portal's.
  overviewIsRigPage: true,
  // Settings on a rig: the rig itself, its projects, the host, access.
  settingsOrder: ["rig", "projects", "host", "access"],
  // A rig's Settings are all its owner's: no group is the administration's alone.
  adminGroup: null,
  // The library's card and its figures: a rig draws its own (below); a
  // portal draws a catalogue of projects, with none of a rig's disk on it.
  libraryCard: null,
  libraryMetrics: null,
  // Rediscover is a rig's own button, boards or none; a portal offers it
  // only to somebody with a rig to rediscover.
  rediscoverOffered: rigCount => true,
  // A page the shell adds to the document, opened by its route: a rig adds none.
  openPage: null,
  // After a person's workspace projects are drawn: a portal offers to add one;
  // a rig's projects are added on its own Projects page.
  afterWorkspaceProjects: null,
  // After the account page is drawn: a portal adds GitHub for repositories.
  // A rig has no accounts.
  afterAccount: null,
  // A rig shows the public farm it could report to; a portal is one.
  farmWorld: true,
  fleet: () => [localRig()],
  rigsOnlineText: (online, total) => "1",
  rigsIdleNote: rigs => "this host",
  runningNote: busyRigs => "on this rig",
  releaseBadge: () => "",
  // This rig's GitHub, which every project depends on: not connected, no
  // longer accepted, or a token about to expire.
  attention: (items, rigs) => {
    const update = lastUpdateView;
    if (update) {
      const state = update.status?.state;
      if (state === "failed") items.push(["bad", "The last update failed", shortDetail(update.status.detail || "See Settings → Rig."), "#configuration"]);
      else if (UPDATING.includes(state) || update.staging) items.push(["warn", `Installing rig software ${update.status?.version || ""}`.trim(), "The service restarts when it is done.", "#configuration"]);
      else if (update.available) items.push(["muted", `Rig software ${update.available.version || ""} is available`.trim(), update.auto ? "It installs on its own when the rig is idle." : "Install it from Settings → Rig, or turn automatic installs on.", "#configuration"]);
    }
    const github = lastGithubSummary;
    if (!github) return;
    const days = github.expires_in_days;
    if (github.configured && !github.connected) items.push(["bad", "GitHub no longer accepts this rig's token", "No project can be added or fetched until it is replaced.", "#configuration"]);
    else if (!github.configured) items.push(["warn", "GitHub is not connected on this rig", "A project is a GitHub repository; connect GitHub in Settings to add one.", "#configuration"]);
    else if (typeof days === "number" && days < 0) items.push(["bad", "This rig's GitHub token has expired", "Make a new one on GitHub and replace it in Settings → Rig.", "#configuration"]);
    else if (typeof days === "number" && days <= 14) items.push(["warn", `This rig's GitHub token expires in ${days} day${days === 1 ? "" : "s"}`, "Make a new one on GitHub and replace it in Settings → Rig.", "#configuration"]);
  },
  allWellNote: "",
  releasesTab: () => { $("releases").innerHTML = '<p class="muted">Releases are what a portal hands its rigs. This farm is deployed with its host.</p>'; },
  listedRigs: (rigs, pending) => rigs,
  rigsEyebrow: "THIS HOST",
  noRigsHint: "",
  rediscoverDisabled: rigBusy => rigBusy,
  rediscoverLabel: "Rediscover",
  addRig: false,
  rediscoverNote: rigBusy => rigBusy ? "Rediscovery waits until the rig is idle." : "",
  boardRigColumn: false,
  localRigPage: true,
  workerQuery: name => "",
  boardRigName: board => board?.worker || "local",
  boardReadable: (state, rigAlone) => state.free && !rigAlone,
  boardCommandLocal: true,
  farmWebhooks: false,
  rigWebhooks: name => false,
  releases: false,
  settingsPortal: false,
  // Settings tabs this shell adds to the three the dashboard has, and
  // what fills each. A rig adds none: what a workspace is, and who owns
  // which rig, is a portal's question.
  settingsTabs: [],
  settingsPanel: id => {},
  // A rig has no accounts: its key is the way in, and it signs nobody in.
  accounts: false,
  // Settings -> Projects for a signed-in person: their workspaces and the
  // projects in each. A rig has no people and no workspaces; it shows what
  // it runs. A portal's shell answers with /api/v1/workspaces.
  workspaceProjects: null,
  // What the portal's shell does on these pages; a rig has nothing to do.
  farmWebhooksLoad: () => {},
  rigWebhooksLoad: name => {},
  pendingActions: rig => "",
  pendingSummary: rig => "",
};
function shell() { return isPortal() && typeof PORTAL_SHELL !== "undefined" ? PORTAL_SHELL : RIG_SHELL; }
function site() { return shell().site; }
function Site() { return shell().Site; }
const $ = id => document.getElementById(id);

async function api(path, options = {}) {
  // A pasted key is sent as a bearer token. A person who signed in has a
  // session cookie instead, which the browser sends by itself; `token` is
  // then the word "session", so that everything that asks "is anybody
  // here" keeps its answer.
  const response = await fetch(path, {...options, credentials: "same-origin", headers: {
    ...(token && token !== "session" ? {"Authorization": `Bearer ${token}`} : {}),
    "Content-Type": "application/json", ...(options.headers || {})}});
  // A path no route matches is answered with the dashboard itself, and
  // parsing that as JSON failed with "Unexpected token '<'" -- true, and no
  // use to anyone reading it. Say what came back instead.
  if (response.status === 401) sessionEnded();
  let body;
  try { body = await response.json(); }
  catch (error) { throw new Error(`The farm has no such thing to show (HTTP ${response.status}, not a JSON answer)`); }
  if (!response.ok) throw new Error(body.error || response.statusText);
  return body;
}

// A session ends on the farm's side -- thirty days, or signed out from
// another tab -- and the tab that had it is told 401. Left as it was, the
// tab kept the dashboard it had, kept polling under a sentinel nothing
// would ever clear, and offered no way back in short of a reload. The
// sentinel goes, the poll stops, and the card comes back with the ways in.
function sessionEnded() {
  if (token !== "session") return;
  token = "";
  clearTimeout(pollTimer);
  $("dashboard").hidden = true; $("login").hidden = false; $("overall").textContent = "LOCKED";
  if ($("sign-out")) $("sign-out").hidden = true;
  if ($("signin-methods")) $("signin-methods").hidden = false;
  $("signin-key").hidden = false;
  showSignInNote("Your session has ended. Sign in again to continue.");
  loadSignInOptions();
}

// A physical suite runs for tens of minutes, so "how long has this been
// going?" is the first question an operator asks. A finished job carries its
// duration from the service; a running one is counted up here, because a
// duration computed server-side is stale the moment it is sent.
function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "";
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m ${String(s).padStart(2, "0")}s`;
  if (m) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}

// A clock that keeps counting between polls: any element carrying
// data-timer-start is rewritten every second by tickDurations().
function liveTimer(startedAt, className = "") {
  if (!startedAt) return "";
  return `<span class="${className}" data-timer-start="${escapeHtml(startedAt)}">${escapeHtml(formatDuration((Date.now() - Date.parse(startedAt)) / 1000))}</span>`;
}

function jobElapsed(job) {
  if (job.duration_seconds !== null && job.duration_seconds !== undefined) return job.duration_seconds;
  if (["running"].includes(job.status) && job.started_at) return (Date.now() - Date.parse(job.started_at)) / 1000;
  return null;
}

function statusClass(value) {
  return ["passed", "ok", "validated"].includes(value) ? "good" : ["failed", "unhealthy"].includes(value) ? "bad" : "warn";
}

function suggestedId(device) { return `${device.target}-${device.mac.replaceAll(":", "").slice(-4)}`; }
function jobSummary(job) {
  if (job.result?.summary) return job.result.summary;
  if (job.status === "failed") return "Pipeline failed — open details";
  return job.result?.revision || job.result?.painlessmesh_sha || job.request?.ref || "";
}
function jobRevision(job) {
  return job.result?.revision || job.result?.painlessmesh_sha || job.request?.resolved_sha || null;
}
// What a job is for. Recorded in the request at submit; read from the
// profile list for a job recorded before that, and from the result last.
// Nothing here assumes one consumer: a job older than profiles is the
// farm's default one, which is what it was.
function jobProfile(job) { return job.request?.profile || job.result?.profile || defaultProfile; }
function jobProject(job) {
  if (job.kind === "inventory") return "Hardware discovery";
  // The profile's label as it is now, so a renamed project is renamed in its
  // history; the label recorded at submit for a profile this farm no longer has.
  return profiles[jobProfile(job)]?.label || job.request?.project || job.result?.project || jobProfile(job);
}
// The version the flashed firmware's build stamped (manifest `version`), when
// it stamped one: the number a person reads, beside the revision of record.
function jobVersion(job) { return typeof job.result?.version === "string" ? job.result.version : ""; }
function jobRepo(job) { return job.request?.repo || repoForProfile(jobProfile(job)); }
// The branch a run was asked for, when it was a branch: a request naming the
// commit itself has no branch to show, and showing the SHA twice says nothing.
function jobBranch(job) {
  // A consumer's CI validates a commit, so its ref is a SHA; the branch it
  // was running for arrives beside it, for display.
  if (job.request?.branch) return job.request.branch;
  const ref = job.request?.ref;
  if (!ref) return "";
  const sha = jobRevision(job);
  if (/^[0-9a-f]{7,40}$/i.test(ref) && (!sha || sha.toLowerCase().startsWith(ref.toLowerCase()))) return "";
  return ref;
}
function jobTargets(job) { return (job.request?.targets || []).join(", "); }
// "owner/name" as a link, from a browsable repository URL.
function repoLink(url) {
  if (!url) return "";
  const withoutScheme = url.replace("https://", "").replace("http://", "");
  const name = withoutScheme.split("/").slice(1).join("/") || withoutScheme;
  return `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(name)}</a>`;
}
function inferredProgress(job) {
  if (job.progress?.length) return job.progress;
  return [{name: "legacy", label: "Legacy pipeline run", status: job.status, summary: job.status === "failed" ? "Run ended before structured stage tracking was available" : ""}];
}

// A commit is a link when the repository it belongs to is known; otherwise
// the short SHA stands on its own rather than pointing nowhere.
// The repository a job's revision belongs to: its profile's. A profile the
// farm no longer has leaves the SHA standing on its own.
function repoForProfile(name) {
  return profiles[name]?.repo || repositories.profiles?.[name] || null;
}
function shaLink(sha, repo = null) {
  if (!sha) return "";
  const short = escapeHtml(String(sha).slice(0, 10));
  return repo && /^[0-9a-f]{40}$/.test(sha) ? `<a class="sha" href="${escapeHtml(repo)}/commit/${escapeHtml(sha)}" target="_blank" rel="noopener">${short}</a>` : `<code>${short}</code>`;
}

// "branch at <sha>", or just the SHA when the ref was one: a sweep submits
// the commit itself, and showing it twice tells the operator nothing.
function revisionLabel(job) {
  const ref = job.request?.ref;
  const sha = jobRevision(job);
  if (!ref && !sha) return "revision unavailable";
  if (!sha) return escapeHtml(ref);
  const version = jobVersion(job);
  if (version) return `<strong>${escapeHtml(version)}</strong> · ${shaLink(sha, jobRepo(job))}`;
  if (!ref || /^[0-9a-f]{7,40}$/i.test(ref) && sha.toLowerCase().startsWith(ref.toLowerCase())) return shaLink(sha, jobRepo(job));
  return `${escapeHtml(ref)} at ${shaLink(sha, jobRepo(job))}`;
}

// ---- the console ------------------------------------------------------------
// What the farm asked of a rig and what came back, oldest first, tailed while
// it is open. The cursor is the id of the last line seen, so a console that
// was closed for ten minutes shows what happened rather than a fresh silence,
// and a reader who scrolls up is not dragged back down by the next line.
const consoleState = {open: false, after: 0, rig: "", follow: true, timer: null, lines: 0, missed: 0};

function consoleLine(event) {
  const when = new Date(event.at);
  const clock = Number.isNaN(when.getTime()) ? "" : when.toLocaleTimeString();
  const level = event.level === "error" ? " error-line" : event.level === "warn" ? " warn-line" : "";
  return `<li class="${event.kind}${level}"><time datetime="${escapeHtml(event.at)}">${escapeHtml(clock)}</time>`
    + `<span class="who">${escapeHtml(event.source || "farm")}</span>`
    + `<span>${escapeHtml(event.text)}</span></li>`;
}

async function pollConsole() {
  clearTimeout(consoleState.timer);
  if (!consoleState.open || document.hidden) return;
  try {
    const rig = consoleState.rig ? `&worker=${encodeURIComponent(consoleState.rig)}` : "";
    const answer = await api(`/api/v1/console?after=${consoleState.after}${rig}&limit=200`);
    const list = $("console-lines");
    if (answer.missed && !consoleState.missed) {
      consoleState.missed = answer.missed;
      list.insertAdjacentHTML("beforeend", `<li class="warn-line"><time></time><span class="who">console</span>`
        + `<span>${escapeHtml(String(answer.missed))} earlier lines are no longer kept</span></li>`);
    }
    if (answer.events.length) {
      const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 60;
      list.insertAdjacentHTML("beforeend", answer.events.map(consoleLine).join(""));
      consoleState.after = answer.cursor;
      consoleState.lines += answer.events.length;
      // A tail keeps what fits on screen and a good deal more, not a session's
      // worth of DOM: the farm keeps the record, this is a window on it.
      while (list.children.length > 600) list.removeChild(list.firstElementChild);
      if (consoleState.follow && atBottom) list.scrollTop = list.scrollHeight;
      $("console-note").textContent = "";
    } else if (!consoleState.lines) {
      $("console-note").textContent = "nothing yet";
    }
    $("console").classList.add("live");
  } catch (error) {
    $("console-note").textContent = error.message;
    $("console").classList.remove("live");
  }
  consoleState.timer = setTimeout(pollConsole, consoleState.open ? 1500 : 10000);
}

function openConsole(rig) {
  const drawer = $("console");
  if (rig !== undefined && rig !== consoleState.rig) {
    consoleState.rig = rig;
    $("console-rig").value = rig;
    // A different rig is a different conversation: start it from what the
    // farm still has rather than from where the last one had got to.
    consoleState.after = 0;
    consoleState.lines = 0;
    consoleState.missed = 0;
    $("console-lines").innerHTML = "";
  }
  consoleState.open = true;
  drawer.querySelector(".console-tools").hidden = false;
  $("console-lines").hidden = false;
  $("console-toggle").setAttribute("aria-expanded", "true");
  document.body.classList.add("console-open");
  pollConsole();
}

function closeConsole() {
  consoleState.open = false;
  clearTimeout(consoleState.timer);
  $("console").classList.remove("live");
  $("console").querySelector(".console-tools").hidden = true;
  $("console-lines").hidden = true;
  $("console-toggle").setAttribute("aria-expanded", "false");
  document.body.classList.remove("console-open");
}

// Changing the channel in the form changes what it asks for.
document.addEventListener("change", event => {
  if (!event.target.classList?.contains("channel-kind")) return;
  rigPage.channelKind = event.target.value;
  forgetRendered($("rig-channels"));
  renderSection($("rig-channels"), rigChannels(rigPage.detail || {}));
  $("rig-channels").querySelector('input[name="secret"]')?.focus();
});

document.addEventListener("click", async event => {
    const target = event.target;
    const has = name => target.classList?.contains(name);
    if (has("farm-notify-open")) {
      // The channel this button belongs to, or "new": a farm may have several.
      const id = target.dataset.id || "";
      farmNotifyEditing = id || "new";
      farmNotifyKind = (farmNotify?.channels || []).find(item => item.id === id)?.channel || "telegram";
      forgetRendered($("farm-notify"));
      renderFarmNotify();
      $("farm-notify").querySelector('input[name="credential"]')?.focus();
      return;
    }
    if (has("farm-notify-cancel")) {
      farmNotifyEditing = false;
      forgetRendered($("farm-notify"));
      renderFarmNotify();
      return;
    }
    if (has("farm-notify-test")) {
      try {
        const outcome = await api("/api/v1/farm/notify/test",
                                  {method: "POST", body: JSON.stringify({id: target.dataset.id || ""})});
        alert(outcome.ok ? "Delivered." : `Not delivered: ${outcome.error || "the channel refused it"}`);
        await loadFarmNotify();
      } catch (error) { alert(`Test refused: ${error.message}`); }
      return;
    }
    if (has("farm-notify-off")) {
      const id = target.dataset.id || "";
      const rest = (farmNotify?.channels || []).length - 1;
      if (!confirm(rest > 0
        ? `Remove this channel? The farm still sends through ${rest} other.`
        : "Remove this channel? Nobody is told when a rig goes quiet.")) return;
      try {
        await api(`/api/v1/farm/notify${id ? `?id=${encodeURIComponent(id)}` : ""}`, {method: "DELETE"});
        await loadFarmNotify();
      } catch (error) { alert(`Refused: ${error.message}`); }
      return;
    }
  });

document.addEventListener("change", event => {
  if (!event.target.classList?.contains("farm-notify-kind")) return;
  farmNotifyKind = event.target.value;
  forgetRendered($("farm-notify"));
  renderFarmNotify();
  $("farm-notify").querySelector('input[name="credential"]')?.focus();
});

document.addEventListener("submit", async event => {
  const form = event.target;
  if (form.id !== "farm-notify-form") return;
  event.preventDefault();
  const kind = form.elements.kind.value;
  let credential = form.elements.credential.value.trim();
  form.elements.credential.value = "";
  const body = {channel: kind, credential, enabled: true};
  // An id means this replaces that channel rather than adding another.
  if (form.dataset.id) body.id = form.dataset.id;
  if (kind === "telegram") body.chat_id = (form.elements.chat_id?.value || "").trim();
  else if (kind === "webhook") body.format = form.elements.format?.value || "slack";
  const button = form.querySelector("button[type=submit]");
  button.disabled = true; button.textContent = "Saving…";
  try {
    farmNotify = await api("/api/v1/farm/notify", {method: "POST", body: JSON.stringify(body)});
    farmNotifyEditing = false;
    forgetRendered($("farm-notify"));
    renderFarmNotify();
  } catch (error) {
    alert(`Not saved: ${error.message}`);
    button.disabled = false; button.textContent = "Save";
  } finally { credential = ""; }
});


// ---- where events go ---------------------------------------------------------
// A notification is for a person. This is the other audience: somebody's
// software, sent the same facts whole and signed, in the shape Alteriom's
// webhook connector already sends (docs/webhooks.md). One renderer, because a
// subscription for the fleet and a subscription for one rig differ only in
// what they are allowed to ask for.

let farmWebhooks = null;
let webhookEditing = null;   // null, or the scope whose form is open

// Whether the key this page holds administers this rig: its owner, or the
// farm's admin. A rig nobody owns is the farm's until somebody is named.
// A key administers every rig: it is only ever created by an admin, for a
// job that needs it. A person administers their own -- the person who
// administers the platform included, because administering a platform is
// releases, keys and access, not ownership of everybody's hardware. The
// fleet is a page they go to on purpose (docs/public-release-plan.md,
// phase 4). `you.account` is what tells the two apart: it is there when
// somebody signed in and absent when a key was pasted.
function ownsRig(rig) {
  if (isAdmin() && !you?.account) return true;
  return Boolean(rig?.owner) && rig.owner === you?.name;
}

function setUpConsole() {
  $("console").hidden = false;
  $("console-toggle").addEventListener("click", () => (consoleState.open ? closeConsole() : openConsole()));
  $("console-rig").addEventListener("change", event => openConsole(event.target.value));
  $("console-follow").addEventListener("click", event => {
    consoleState.follow = !consoleState.follow;
    event.currentTarget.setAttribute("aria-pressed", String(consoleState.follow));
    event.currentTarget.textContent = consoleState.follow ? "Following" : "Paused";
    if (consoleState.follow) $("console-lines").scrollTop = $("console-lines").scrollHeight;
  });
  $("console-copy").addEventListener("click", async () => {
    const text = [...$("console-lines").children].map(line => line.textContent.replace(/\s+/g, " ").trim()).join("\n");
    try { await navigator.clipboard.writeText(text); $("console-note").textContent = "copied"; }
    catch { $("console-note").textContent = "the browser would not copy"; }
  });
  $("console-clear").addEventListener("click", () => {
    $("console-lines").innerHTML = "";
    consoleState.lines = 0;
    $("console-note").textContent = "";
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) pollConsole(); });
}

// The rigs the console can be filtered to, as the fleet becomes known.
function consoleRigs(names) {
  const select = $("console-rig");
  if (!select) return;
  const known = [...select.options].map(option => option.value).filter(Boolean);
  if (known.join() === names.join()) return;
  select.innerHTML = `<option value="">All rigs</option>`
    + names.map(name => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join("");
  select.value = consoleState.rig;
}

// The progress bar's width is set from script: the service's content
// security policy allows no inline style attribute, so a width written into
// the markup is silently dropped.
function setProgress(root, fraction) {
  root.querySelectorAll(".progress-track > span").forEach(bar => { bar.style.width = `${Math.round(Math.max(0, Math.min(1, fraction)) * 100)}%`; });
}

// Whether this caller is confined to their own workspace. Not simply "is an
// account": an admin signs in as an account like anybody else, and the farm
// authorizes them before it asks about workspaces (api_keys.allowed), so
// hiding the farm-wide half from them takes away the whole dashboard they
// are the main user of.
function workspaceOnly() {
  return document.body.dataset.caller === "account" && document.body.dataset.role !== "admin";
}

function showPanel(name, updateHash = true, suffix = "", {as = null} = {}) {
  const wanted = document.querySelector(`.page[data-page="${CSS.escape(name)}"]`);
  // A document without a fleet overview (a rig's) lands on its rig page.
  const fallback = document.querySelector('.page[data-page="overview"]') ? "overview" : "rig";
  // A page the caller is not offered -- a farm-wide one to an account, an
  // admin's to anybody else -- lands them home instead of on a blank page.
  const withheld = wanted && ((workspaceOnly() && wanted.classList.contains("farm-wide"))
    || (document.body.dataset.role !== "admin" && wanted.classList.contains("admin-only")));
  const target = wanted && !withheld ? name : fallback;
  document.querySelectorAll(".page").forEach(page => { const active = page.dataset.page === target; page.hidden = !active; page.classList.toggle("active", active); });
  // `as` is the address and the nav item this page stands for: on a rig the
  // rig page is #overview, and Overview is lit.
  const shown = as || (target === "rig" && fallback === "rig" && !suffix ? "overview" : target);
  const lit = shown === "run" ? "runs" : ["artifact", "storage"].includes(shown) ? "artifacts" : ["rig", "board"].includes(shown) ? "rigs" : shown;
  document.querySelectorAll(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.panel === lit));
  if (updateHash) {
    history.replaceState(null, "", `#${shown}${suffix}`);
    lastRouted = location.hash;
  }
}

// The address the router last acted on. A fragment change can arrive as a
// `popstate`, a `hashchange`, or both, depending on the browser; routing once
// per address keeps a page from loading twice for one click.
let lastRouted = null;
let settingsTab = "general";
// A settings tab that was asked for and is not there yet. Which shell this
// is only becomes known when the status arrives, so a link straight to a tab
// the shell adds -- a portal's Workspaces -- is asked for before anything
// knows it exists. Kept here rather than read back off the hash, which has
// been rewritten to the tab that was shown instead.
let settingsWanted = null;

function routeFromLocation({force = false} = {}) {
  if (!force && location.hash === lastRouted) return;
  lastRouted = location.hash;
  openRoute(parseRoute(location.hash), {updateHash: false});
}

// Go somewhere in the dashboard. It used to assign to location.hash, which
// only changes the fragment: whether the page followed depended on the
// browser also firing `popstate` for it, which Chromium does and nothing
// promises. This makes a history entry -- so Back returns -- and routes now.
function navigateTo(href) {
  if (location.hash !== href) history.pushState(null, "", href);
  routeFromLocation({force: true});
}

// `#runs`, `#run/<id>`, `#artifacts`: a page and, for the pages that show one
// thing, which thing. Parsed in one place so a link, a reload and a click all
// arrive the same way.
// The menu names what an operator does -- Rigs, Firmware, Insights,
// Settings -- and older links keep working.
const ROUTE_ALIASES = {hardware: "rigs", firmware: "artifacts", insights: "statistics", settings: "configuration"};
function parseRoute(hash) {
  const [raw, id] = hash.replace(/^#/, "").split("/", 2);
  const name = ROUTE_ALIASES[raw] || raw;
  return {name: name || "overview", id: id ? decodeURIComponent(id) : null};
}

// Show whatever a route names, and load what that page needs. `updateHash`
// is false when the browser already changed the URL for us -- a reload, or
// the back button -- so the router does not fight it.
function openRoute(route, {updateHash = true} = {}) {
  if (route.name === "overview" && shell().overviewIsRigPage) {
    // The rig's own page, kept at #overview: the address a rig opens on.
    showPanel("rig", updateHash, "", {as: "overview"});
    if (token) showRig("local");
    return;
  }
  if (route.name === "account") {
    showPanel("account", updateHash);
    if (token) loadAccount();
    return;
  }
  if (route.name === "run" && route.id) {
    showPanel("run", updateHash, `/${route.id}`);
    if (token) showJob(route.id, {force: true});
    return;
  }
  if (route.name === "artifact" && route.id) {
    showPanel("artifact", updateHash, `/${route.id}`);
    if (token) showBundlePage(route.id);
    return;
  }
  if (route.name === "rig" && route.id) {
    showPanel("rig", updateHash, `/${encodeURIComponent(route.id)}`);
    if (token) showRig(route.id);
    return;
  }
  if (route.name === "board" && route.id) {
    showPanel("board", updateHash, `/${encodeURIComponent(route.id)}`);
    if (token) showBoard(route.id);
    return;
  }
  if (["rigs", "boards", "releases"].includes(route.name)) {
    showPanel("rigs", false);
    showRigsTab(route.name, updateHash);
    return;
  }
  if (route.name === "artifacts") {
    showPanel("artifacts", false);
    showFirmwareTab(route.id || "library", updateHash);
    return;
  }
  if (route.name === "configuration") {
    showPanel("configuration", false);
    showSettingsTab(route.id || "general", updateHash);
    return;
  }
  if (route.name === "storage" && route.id) {
    showPanel("storage", updateHash, `/${route.id}`);
    if (token) loadStorageDetail(route.id);
    return;
  }
  showPanel(route.name, updateHash);
  if (!token) return;
  if (route.name === "statistics") loadStatistics(Number(route.id) || statsDays);
  // A page the shell added -- a portal's Admin -- loads through the shell.
  if (shell().openPage) shell().openPage(route.name, route.id);
}

// ---- Markdown -------------------------------------------------------------
// The generated report is Markdown. It used to be shown as its source in a
// <pre>, which reads like a diff of itself. This renders the subset the
// report generator writes — headings, paragraphs, lists, tables, fenced
// code, emphasis, links — and nothing else: every line is HTML-escaped
// before any markup is recognised, links are kept to http(s), and images
// are not rendered at all, so a report can never carry script into the
// dashboard.
function inlineMarkdown(text) {
  return text
    .replace(/`([^`]+)`/g, (_, code) => `<code>${code}</code>`)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

function renderMarkdown(source) {
  const lines = escapeHtml(source).replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;
  const isTableRow = line => /^\s*\|.*\|\s*$/.test(line);
  const isTableSeparator = line => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*$/.test(line)) { i++; continue; }
    const fence = line.match(/^\s*```/);
    if (fence) {
      const code = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]);
      i++;
      out.push(`<pre><code>${code.join("\n")}</code></pre>`);
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) { out.push(`<h${heading[1].length}>${inlineMarkdown(heading[2])}</h${heading[1].length}>`); i++; continue; }
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { out.push("<hr>"); i++; continue; }
    if (isTableRow(line) && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
      const cells = row => row.trim().replace(/^\||\|$/g, "").split("|").map(cell => inlineMarkdown(cell.trim()));
      const head = cells(line);
      i += 2;
      const body = [];
      while (i < lines.length && isTableRow(lines[i])) body.push(cells(lines[i++]));
      out.push(`<div class="table-wrap"><table><thead><tr>${head.map(cell => `<th>${cell}</th>`).join("")}</tr></thead><tbody>${body.map(row => `<tr>${row.map(cell => `<td>${cell}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
      continue;
    }
    const list = line.match(/^\s*([-*+]|\d+\.)\s+/);
    if (list) {
      const ordered = /\d/.test(list[1]);
      const items = [];
      while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*([-*+]|\d+\.)\s+/, ""));
      out.push(`<${ordered ? "ol" : "ul"}>${items.map(item => `<li>${inlineMarkdown(item)}</li>`).join("")}</${ordered ? "ol" : "ul"}>`);
      continue;
    }
    if (/^\s*&gt;\s?/.test(line)) {
      const quote = [];
      while (i < lines.length && /^\s*&gt;\s?/.test(lines[i])) quote.push(lines[i++].replace(/^\s*&gt;\s?/, ""));
      out.push(`<blockquote>${inlineMarkdown(quote.join(" "))}</blockquote>`);
      continue;
    }
    const paragraph = [];
    while (i < lines.length && !/^\s*$/.test(lines[i]) && !/^(#{1,6}\s|\s*```|\s*([-*+]|\d+\.)\s|\s*&gt;)/.test(lines[i]) && !(isTableRow(lines[i]) && isTableSeparator(lines[i + 1] || ""))) paragraph.push(lines[i++]);
    out.push(`<p>${inlineMarkdown(paragraph.join(" "))}</p>`);
  }
  return `<div class="md">${out.join("")}</div>`;
}

// ---- Run detail -------------------------------------------------------------
const ARTIFACT_LABELS = {
  manifest: "Bundle manifest", junit: "JUnit results", preflight: "Preflight", board_health: "Board health",
  report_markdown: "Report (Markdown)", report_json: "Report (JSON)", runs: "Test records", log: "Full log",
};

// Files are fetched with the token and opened from a blob: a plain link
// cannot carry the Authorization header, and a token must never go in a URL.
// One path for every file the dashboard hands out -- a run's artifacts and a
// bundle's files alike.
async function fetchFromApi(path) {
  // The same rule as api(): a pasted key goes as a bearer token, a session
  // goes as its cookie. Sent as `Bearer session`, every file -- an image,
  // an archive, a serial log -- was refused to a person who had signed in.
  const response = await fetch(path, {credentials: "same-origin",
    headers: token && token !== "session" ? {"Authorization": `Bearer ${token}`} : {}});
  if (response.status === 401) sessionEnded();
  if (!response.ok) { let message = response.statusText; try { message = (await response.json()).error || message; } catch (_) {} throw new Error(message); }
  return response;
}

// Saved under the name the service gives it: a firmware image or an archive
// has nothing to look at.
async function saveResponse(response, fallbackName) {
  const filename = (response.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/)?.[1] || fallbackName;
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url; anchor.download = filename; document.body.appendChild(anchor); anchor.click(); anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

// Opened in a new tab. Browsers render text types inline; Markdown is shown
// as text, since nothing renders it natively.
async function viewResponse(response) {
  const type = response.headers.get("Content-Type") || "text/plain";
  const shown = type.startsWith("text/markdown") ? "text/plain; charset=utf-8" : type;
  const url = URL.createObjectURL(new Blob([await response.arrayBuffer()], {type: shown}));
  window.open(url, "_blank", "noopener");
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

async function openArtifact(jobId, name) {
  const response = await fetchFromApi(`/api/v1/jobs/${jobId}/artifacts/${encodeURIComponent(name)}`);
  const type = response.headers.get("Content-Type") || "text/plain";
  return type.startsWith("application/octet-stream") ? saveResponse(response, name.replace(":", "-")) : viewResponse(response);
}

function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return "";
  if (bytes >= 1073741824) return `${(bytes / 1073741824).toFixed(1)} GB`;
  if (bytes >= 1048576) return `${(bytes / 1048576).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

// Every file the run left, in three groups: the reports and records, one
// serial capture per board (and its preflight capture), and one flash image
// per family built. Each is a button that opens it; a run claims nothing
// here that cannot be opened.
function renderArtifacts(job) {
  const entries = Object.entries(job.artifacts || {}).filter(([, artifact]) => artifact.available);
  if (!entries.length) return "";
  const button = (name, label, artifact) => `<button type="button" class="artifact open-artifact" data-name="${escapeHtml(name)}" title="Open ${escapeHtml(label)}${artifact.bytes ? ` (${formatBytes(artifact.bytes)})` : ""}">${escapeHtml(label)}${artifact.bytes ? ` <small>${escapeHtml(formatBytes(artifact.bytes))}</small>` : ""}</button>`;
  const groups = [
    ["Reports and records", entries.filter(([name]) => !name.includes(":")).map(([name, a]) => button(name, ARTIFACT_LABELS[name] || name.replaceAll("_", " "), a))],
    ["Serial logs", entries.filter(([name]) => name.startsWith("serial:")).map(([name, a]) => { const stem = name.slice(7); return button(name, stem.endsWith(".preflight") ? `${stem.slice(0, -10)} · preflight` : stem, a); })],
    ["Firmware images", entries.filter(([name]) => name.startsWith("firmware:")).map(([name, a]) => button(name, `${familyLabel(name.slice(9))} flash image`, a))],
  ].filter(([, buttons]) => buttons.length);
  return groups.map(([title, buttons]) => `<div class="artifacts"><strong>${escapeHtml(title)}</strong>${buttons.join("")}</div>`).join("");
}

// Which bundle a run flashed, as a way into the Artifacts page -- or, when
// its images were pruned, a sentence saying so, rather than a manifest
// button that no longer opens.
// What retention took from a run: said on its page, not left as captures
// that silently are not there.
function renderEvidenceNote(job) {
  const removed = job.evidence_removed || {};
  const notes = [];
  if (removed.serial) notes.push(`Serial and broker captures removed by retention ${escapeHtml(new Date(removed.serial.removed_at).toLocaleString())}; the report and records are kept.`);
  if (removed.log) notes.push(`Job log removed by retention ${escapeHtml(new Date(removed.log.removed_at).toLocaleString())}.`);
  return notes.map(note => `<p class="muted">${note}</p>`).join("");
}

function renderBundleNote(job) {
  const bundle = job.bundle;
  if (!bundle) return "";
  if (!bundle.available) {
    return bundle.removed_at
      ? `<p class="muted">Firmware images pruned ${escapeHtml(new Date(bundle.removed_at).toLocaleString())}${bundle.removed_reason ? ` (${escapeHtml(bundle.removed_reason)})` : ""}. The reports, logs and serial captures are kept.</p>`
      : "";
  }
  const reused = bundle.id !== job.id;
  // A supplied bundle was built by no run on the farm: say whose CI it was,
  // not "the bundle run X built" about a run that never existed.
  const supplied = bundle.source?.kind === "supplied";
  const origin = supplied
    ? `Flashed a bundle supplied by ${bundle.source.repo ? repoLink(bundle.source.repo) : "a producer"}${bundle.source.run_id ? ` ${producerRunLink(bundle.source)}` : ""}`
    : reused ? `Flashed the bundle run ${escapeHtml(bundle.id.slice(0, 8))} built` : "Built its own bundle";
  return `<p class="muted">${origin} · <a href="#artifact/${escapeHtml(bundle.id)}">open bundle ${escapeHtml(bundle.id.slice(0, 8))}</a></p>`;
}

function renderReport(job) {
  const report = job.report;
  if (!report) return "<p class=muted>No machine-readable hardware report was produced for this run.</p>";
  const verdicts = report.verdicts || {};
  const capabilities = Object.entries(report.capabilities || {});
  const classes = Object.entries(report.failure_classes || {});
  const reasons = Object.entries(report.hil_only_reasons || {});
  return `<h3>Physical validation report</h3><div class="report-metrics">
    <article><span>Gate</span><strong class="${statusClass(report.validation_gate)}">${escapeHtml((report.validation_gate || "unknown").toUpperCase())}</strong></article>
    <article><span>Tests</span><strong>${escapeHtml(report.total_runs || 0)}</strong></article><article><span>Passed</span><strong class="good">${escapeHtml(verdicts.passed || 0)}</strong></article><article><span>Failed</span><strong class="${(verdicts.failed || verdicts.error) ? "bad" : ""}">${escapeHtml((verdicts.failed || 0) + (verdicts.error || 0))}</strong></article>
    </div>
    ${report.bug_catch_delta !== undefined ? `<p class="muted">Bug-catch delta vs compile-only CI: <strong>${escapeHtml(report.bug_catch_delta)}</strong>${classes.length ? ` · failure classes: ${classes.map(([k, v]) => `${escapeHtml(k)} ×${escapeHtml(v)}`).join(", ")}` : ""}${reasons.length ? ` · HIL-only: ${reasons.map(([k, v]) => `${escapeHtml(k)} ×${escapeHtml(v)}`).join(", ")}` : ""}</p>` : ""}
    <div class="table-wrap"><table><thead><tr><th>Capability</th><th>Status</th><th>Evidence</th></tr></thead><tbody>${capabilities.map(([name, item]) => `<tr><td><code>${escapeHtml(name)}</code></td><td><span class="state ${statusClass(item.status)}">${escapeHtml(item.status)}</span></td><td>${escapeHtml((item.tests || []).length)} test(s)</td></tr>`).join("")}</tbody></table></div>
    ${job.report_markdown ? `<details class="report-full" open><summary>Full generated report</summary>${renderMarkdown(job.report_markdown)}</details>` : ""}`;
}

function renderTimings(job) {
  const stages = (job.progress || []).filter(s => s.started_at);
  const rows = [
    ["Queued", job.created_at && new Date(job.created_at).toLocaleString()],
    ["Started", job.started_at && new Date(job.started_at).toLocaleString()],
    ["Finished", job.finished_at && new Date(job.finished_at).toLocaleString()],
    ["Waited", job.queued_seconds != null ? formatDuration(job.queued_seconds) : null],
  ].filter(([, v]) => v);
  const took = job.status === "running" && job.started_at
    ? `<div><small>Running for</small>${liveTimer(job.started_at)}</div>`
    : `<div><small>Took</small><span>${escapeHtml(formatDuration(jobElapsed(job)))}</span></div>`;
  const perStage = stages.map(s => {
    const value = s.finished_at ? escapeHtml(formatDuration((Date.parse(s.finished_at) - Date.parse(s.started_at)) / 1000)) : liveTimer(s.started_at);
    return `<div><small>${escapeHtml(s.label || s.name)}</small>${s.finished_at ? `<span>${value}</span>` : value}</div>`;
  }).join("");
  return `<h3>Timing</h3><div class="detail-grid">${rows.map(([l, v]) => `<div><small>${escapeHtml(l)}</small><span>${escapeHtml(v)}</span></div>`).join("")}${took}</div>${perStage ? `<h3>Stage durations</h3><div class="detail-grid">${perStage}</div>` : ""}`;
}

function renderSimulation(job) {
  const evidence = job.result?.simulation || job.request?.simulation;
  // Simulation is a parameter of the job. A run that carried none has
  // nothing to say here -- not a note about missing simulator evidence, which
  // read as a gap in every run of a product that has no simulator.
  if (!evidence) return "";
  const protocol = evidence.protocol_sim || {};
  const mesh = evidence.mesh_sim || {};
  return `<h3>Simulation supplied with this run</h3><div class="evidence-grid">
    <article><span class="state ${statusClass(protocol.status)}">${escapeHtml(protocol.status || "unknown")}</span><strong>Protocol simulation</strong><p>${escapeHtml(protocol.summary || "")}</p><small>${escapeHtml(protocol.tests || 0)} runs · ${escapeHtml((protocol.capabilities || []).length)} capabilities</small></article>
    <article><span class="state ${statusClass(mesh.status)}">${escapeHtml(mesh.status || "unknown")}</span><strong>Behavioural mesh simulation</strong><p>${escapeHtml(mesh.summary || "")}</p><small>${escapeHtml((mesh.scenarios || []).join(" · "))}</small></article>
    </div>`;
}

// Cancel, or promote, from wherever a job is shown. Confirmed for a running
// run: cancelling one throws away tens of minutes of rig time. A user key may
// cancel the runs it started and nothing else, and reorders nothing.
function isAdmin() { return !you || you.role === "admin"; }
function jobActionButtons(job, {promotable = true} = {}) {
  const buttons = [];
  if (job.status === "queued" && promotable && isAdmin()) buttons.push(`<button class="secondary job-promote" data-id="${escapeHtml(job.id)}" title="Run this next, ahead of everything else queued">Run next</button>`);
  const mayCancel = isAdmin() || (job.request?.submitted_by && job.request.submitted_by === you?.name);
  if (["queued", "running"].includes(job.status) && mayCancel) buttons.push(`<button class="danger job-cancel" data-id="${escapeHtml(job.id)}" data-status="${escapeHtml(job.status)}">${job.status === "running" ? "Cancel run" : "Cancel"}</button>`);
  // A finished run is the operator's to keep or not: deleting it takes its
  // evidence, its log and its record. On a rig; a portal keeps a rig's
  // history for it.
  if (!["queued", "running"].includes(job.status) && isAdmin() && shell().projectsAreOwn && !promotableOnlyList) {
    buttons.push(`<button class="danger job-delete" data-id="${escapeHtml(job.id)}" title="Delete this run: its evidence, log and record">Delete run</button>`);
  }
  return buttons.join("");
}
let promotableOnlyList = false;

function renderYou(identity) {
  you = identity || null;
  if (you) document.body.dataset.role = you.role; else delete document.body.dataset.role;
  // And which kind of caller: a key or a person. A `user` key is an operator
  // credential and reads the whole farm; an account reads its own workspace,
  // so the two are told apart here once and the stylesheet does the rest.
  if (you && you.account) document.body.dataset.caller = "account";
  else delete document.body.dataset.caller;
  // The caller arrives with the first status, after the router has acted on
  // the address the page opened at -- so a tab that is not this caller's is
  // left behind here rather than underneath them.
  if (workspaceOnly() && rigsTab === "releases") showRigsTab("rigs");
  // And the page itself, for the same reason: the caller is only known once
  // the first status arrives, by which time the router has already acted on
  // whatever address the page opened at.
  const here = document.querySelector(".page.active");
  if (here && workspaceOnly() && here.classList.contains("farm-wide")) {
    showPanel("overview");
  }
  const badge = $("you");
  if (!badge) return;
  badge.hidden = !you;
  badge.textContent = you ? `${you.account ? you.name : shell().keyLabel(you.name)} · ${you.role}` : "";
  badge.title = !you ? "" : you.account
    ? (you.role === "admin" ? "Your account: the farm's admin"
      : you.role === "guest" ? "Your account, not yet let in: the farm's admin can"
      : "Your account: your rigs, your runs, your events")
    : shell().keyTitle(you.role);
}

function installJobActionHandlers(root = document) {
  root.querySelectorAll(".job-cancel").forEach(button => button.addEventListener("click", async () => {
    const id = button.dataset.id;
    if (button.dataset.status === "running" && !confirm(`Cancel the running job ${id.slice(0, 8)}? It tears down and keeps the evidence it has.`)) return;
    button.disabled = true;
    try { await api(`/api/v1/jobs/${id}/cancel`, {method: "POST", body: "{}"}); await refresh(true); }
    catch (error) { alert(`Cancel failed: ${error.message}`); button.disabled = false; }
  }));
  root.querySelectorAll(".job-promote").forEach(button => button.addEventListener("click", async () => {
    button.disabled = true;
    try { await api(`/api/v1/jobs/${button.dataset.id}/promote`, {method: "POST", body: "{}"}); await refresh(true); }
    catch (error) { alert(`Could not move the job: ${error.message}`); button.disabled = false; }
  }));
}

// Where a run came from and what it touched. The detail used to say a
// project label and a revision; the repository, the branch, the families
// built and the boards used are what an operator asks first.
function runFacts(job) {
  if (job.kind === "inventory") return "";
  const result = job.result || {};
  return `<div class="live-facts">
    <div><small>Project</small><span>${escapeHtml(jobProject(job))}</span></div>
    <div><small>Repository</small><span>${repoLink(jobRepo(job)) || "—"}</span></div>
    <div><small>Branch</small><span>${escapeHtml(jobBranch(job) || "—")}</span></div>
    ${jobVersion(job) ? `<div><small>Version</small><span><strong>${escapeHtml(jobVersion(job))}</strong></span></div>` : ""}
    <div><small>Revision</small><span>${shaLink(jobRevision(job), jobRepo(job)) || "—"}</span></div>
    <div><small>Targets</small><span>${escapeHtml(jobTargets(job) || "—")}</span></div>
    <div><small>Started by</small><span>${actorLink(job.request?.actor) || escapeHtml(job.request?.submitted_by || "—")}${job.request?.actor && job.request?.submitted_by ? ` <small class="muted">via ${escapeHtml(job.request.submitted_by)}</small>` : ""}</span></div>
    <div><small>Boards</small><span>${result.boards ?? "—"}</span></div>
    <div><small>Ran on</small><span>${escapeHtml(job.worker || (farmMode === "standalone" ? "this farm" : "—"))}${job.request?.imported_from ? ` <small class="muted" title="Run on ${escapeHtml(job.request.imported_from)} before it joined the portal, and brought with its history">before the portal</small>` : ""}</span></div>
  </div>`;
}

function renderJob(job) {
  const details = $("log-details");
  const wasOpen = details.open;
  const log = $("job-log");
  const followLog = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  $("job-title").textContent = `${job.kind} · ${job.id.slice(0, 8)}`;
  // The run's own line, under its title: what it validated, so the page
  // says what it is about before any of the evidence is read.
  const branch = jobBranch(job);
  $("job-subtitle").innerHTML = [
    escapeHtml([jobProject(job), jobVersion(job)].filter(Boolean).join(" ")),
    branch ? escapeHtml(branch) : "",
    job.kind === "inventory" ? "" : shaLink(jobRevision(job), jobRepo(job)),
    escapeHtml(jobTargets(job) || ""),
  ].filter(Boolean).join(" · ");
  $("job-actions").innerHTML = jobActionButtons(job);
  $("job-actions").querySelector(".job-delete")?.addEventListener("click", async () => {
    if (!confirm(`Delete run ${job.id.slice(0, 8)}? Its evidence, log and record go; this cannot be undone.`)) return;
    try {
      await api(`/api/v1/jobs/${encodeURIComponent(job.id)}/delete`, {method: "POST", body: "{}"});
    } catch (error) { alert(`This rig refused: ${error.message}`); return; }
    navigateTo("#runs");
    await refresh(true);
  });
  const result = job.result || {};
  const selection = selectionOf(job);
  const partial = selection.tests.length || selection.keyword;
  const failed = failedTestsOf(job);
  const reruns = job.kind === "suite" && !["queued", "running"].includes(job.status)
    ? `<div class="actions">${failed.length ? `<button type="button" class="secondary rerun-failed" title="${escapeHtml(failed.join("\n"))}">Re-run the ${failed.length} failed test${failed.length === 1 ? "" : "s"}</button>` : ""}<button type="button" class="secondary rerun-same">${partial ? "Re-run this selection" : "Re-run the suite"}</button><small class="muted">at the same commit, reusing its artifacts and skipping the flash when the boards still run it</small></div>`
    : "";
  $("job-summary").innerHTML = `<p><span class="state ${statusClass(job.status)}">${escapeHtml(job.status)}</span>${partial ? ' <span class="state warn">PARTIAL</span>' : ""} <strong>${escapeHtml(jobSummary(job))}</strong></p>${runFacts(job)}${partial ? `<p class="muted">Selection: ${selection.tests.length ? selection.tests.map(t => `<code>${escapeHtml(t)}</code>`).join(" ") : "all files"}${selection.keyword ? ` matching <code>${escapeHtml(selection.keyword)}</code>` : ""}</p>` : ""}${result.detail ? `<p class="failure-summary">${escapeHtml(result.detail)}</p>` : ""}${reruns}${renderArtifacts(job)}${renderBundleNote(job)}${renderEvidenceNote(job)}`;
  $("job-summary").querySelectorAll(".open-bundle").forEach(button => button.addEventListener("click", () => openBundle(button.dataset.id)));
  $("job-summary").querySelectorAll(".rerun-failed").forEach(button => button.addEventListener("click", () => { button.disabled = true; rerun(job, failed).catch(error => { alert(`${Site()} refused the run: ${error.message}`); button.disabled = false; }); }));
  $("job-summary").querySelectorAll(".rerun-same").forEach(button => button.addEventListener("click", () => { button.disabled = true; rerun(job, selection.tests, selection.keyword).catch(error => { alert(`${Site()} refused the run: ${error.message}`); button.disabled = false; }); }));
  $("simulation").innerHTML = renderSimulation(job);
  $("pipeline").innerHTML = `<h3>Pipeline</h3><div class="pipeline">${inferredProgress(job).map(stage => `<article class="pipeline-stage ${escapeHtml(stage.status)}"><span class="stage-dot"></span><div><small class="stage-group">${escapeHtml(stage.group || "pipeline")}</small><strong>${escapeHtml(stage.label)}</strong><small><span class="stage-status">${escapeHtml(stage.status)}</span>${escapeHtml(stage.summary || "")}${stage.status === "running" && stage.started_at ? ` · ${liveTimer(stage.started_at)}` : ""}</small></div></article>`).join("")}</div>`;
  $("timing").innerHTML = renderTimings(job);
  $("report").innerHTML = renderReport(job);
  log.textContent = job.log_tail || "No log output available.";
  details.open = wasOpen;
  if (followLog) log.scrollTop = log.scrollHeight;
  $("job-summary").querySelectorAll(".open-artifact").forEach(button => button.addEventListener("click", () => openArtifact(job.id, button.dataset.name).catch(error => alert(`Could not open the artifact: ${error.message}`))));
  installJobActionHandlers(document.querySelector('.page[data-page="run"]'));
}

// The one place the page scrolls: an operator asking to look at something --
// View on a run, Details on a bundle. Live polling never calls it, so a
// refresh never moves the page out from under someone reading it.
function bringIntoView(element) {
  element.scrollIntoView({behavior: "smooth", block: "start"});
}

async function showJob(jobId, {focus = false, force = false} = {}) {
  const changed = selectedJobId !== jobId;
  selectedJobId = jobId;
  let job;
  try {
    job = await api(`/api/v1/jobs/${jobId}`);
  } catch (error) {
    // A link to a run the farm no longer has -- pruned history, a typo in a
    // pasted URL -- says so on the page instead of leaving the last run's
    // details under someone else's id.
    selectedJobId = null;
    $("job-title").textContent = "No such run";
    $("job-subtitle").textContent = jobId;
    $("job-summary").innerHTML = `<p class="failure-summary">${escapeHtml(error.message)}</p>`;
    ["timing", "simulation", "pipeline", "report"].forEach(id => { $(id).innerHTML = ""; });
    $("job-actions").innerHTML = "";
    return;
  }
  const signature = JSON.stringify(job);
  selectedJob = job;
  if (force || changed || signature !== detailSignature) { detailSignature = signature; renderJob(job); }
  if (focus) openRun(jobId);
}

// Go to a run's own page. Everything that opens a run goes through here, so
// the URL always describes what is on screen.
function openRun(jobId) {
  showPanel("run", true, `/${jobId}`);
  bringIntoView(document.querySelector('.page[data-page="run"]'));
}

function familyLabel(target) { return target.replace(/^esp32-/, "").toUpperCase(); }

// One checkbox per artifact family the service knows (its TARGETS),
// pre-checked for every family with a connected board: the service refuses a
// run whose selection leaves a connected board without an artifact. Rebuilt
// only when the family list or the connected set changes, so an operator's
// own toggles survive the polling refresh.
function renderProfiles() {
  const select = document.getElementById("profile-select");
  if (!select) return;
  // On a rig the form runs projects; the health check is run from Boards.
  // A rig with no project yet says so rather than offering the health check
  // as if it were one.
  const every = Object.keys(profiles).sort();
  const names = shell().projectsAreOwn ? every.filter(name => name !== "canary") : every;
  const submit = $("suite-submit");
  if (!names.length) {
    if (every.length) {
      select.innerHTML = '<option value="">No project on this rig yet</option>';
      select.dataset.signature = "none";
      if (submit) submit.disabled = true;
      const note = $("bundle-note");
      if (note) note.innerHTML = 'Add a project under <a href="#configuration/projects">Settings → Projects</a>; the Rig Health Check is run from <a href="#rigs">Boards</a>.';
      renderProjectStrip("");
    }
    return;
  }
  // Opens on the rig's default project; after that the choice is the operator's.
  const chosen = select.value && names.includes(select.value) ? select.value
    : names.includes(defaultProfile) ? defaultProfile : names[0];
  const signature = JSON.stringify([names, chosen]);
  if (select.dataset.signature !== signature) {
    select.dataset.signature = signature;
    select.innerHTML = names.map(name =>
      `<option value="${escapeHtml(name)}"${name === chosen ? " selected" : ""}>${escapeHtml(profiles[name].label || name)}</option>`
    ).join("");
  }
  renderProjectStrip(chosen);
  loadBundleChoices(chosen);
}

// What the chosen project is configured with, under the picker, so the form
// reads as that project's: its repository and default ref, where its suite
// is, which boards a run takes, where its firmware comes from. The families
// below follow it (renderFamilies), and the test picker says when the suite
// is not on this host to be listed.
let projectStripFor = null;
function renderProjectStrip(name) {
  const strip = $("project-strip");
  if (!strip) return;
  const spec = profiles[name];
  if (!spec) { strip.hidden = true; projectStripFor = null; return; }
  const signature = JSON.stringify([name, spec]);
  if (projectStripFor === signature) return;
  projectStripFor = signature;
  strip.hidden = false;
  const takes = (spec.needs || []).length ? profileTakes(spec) : `the whole bench (${spec.min_boards || 1}+ boards)`;
  const supply = spec.supply_workflow ? `<code>${escapeHtml(spec.supply_workflow)}</code>${spec.supply_repo && spec.supply_repo !== spec.repo ? ` in ${repoLink(spec.supply_repo)}` : ""}` : '<span class="bad">no producer</span>';
  strip.innerHTML = `<span>${spec.repo ? repoLink(spec.repo) : "—"} <small class="muted">default <code>${escapeHtml(spec.default_ref || "main")}</code></small></span>
    <span><small class="muted">suite</small> <code>${escapeHtml(spec.suite_path || "")}</code>${spec.location === "consumer" ? ' <small class="muted">checked out per run</small>' : ""}</span>
    <span><small class="muted">takes</small> ${escapeHtml(takes)}</span>
    <span><small class="muted">firmware from</small> ${supply}</span>
    <a href="#configuration/projects/${escapeHtml(name)}">Project page</a>`;
  const note = $("test-selection-note");
  if (note && spec.location === "consumer") note.title = "This project's suite arrives with its checkout, so its tests are named by hand here.";
  renderFamilies(familyArgs.targets, familyArgs.inv);
}

// ---- The bundle a run flashes ------------------------------------------------
// The farm does not build firmware. A run started here flashes a bundle the
// farm holds -- one a project's CI built and handed over -- so the form offers
// those for the chosen profile, newest first, and the bundle is what decides
// the commit. Listed when the profile changes and at most once a minute
// besides: listing the store is not free on the Pi, and the status poll
// runs every few seconds.
const BUNDLE_CHOICES = 50;
const bundleChoices = {profile: null, bundles: [], shown: [], matched: 0, listed: 0, stale: 0, error: null, loading: false, loadedAt: 0, signature: ""};
// The bundle "Run with this bundle" asked for, which may be older than the
// newest BUNDLE_CHOICES and so not in the list at all.
let preferredBundle = null;

async function loadBundleChoices(profile, {force = false} = {}) {
  if (!token || !profile) return;
  if (bundleChoices.profile !== profile) {
    Object.assign(bundleChoices, {profile, bundles: [], matched: 0, listed: 0, stale: 0, error: null, loading: false, loadedAt: 0});
    renderBundleChoices();
  }
  if (bundleChoices.loading || (!force && Date.now() - bundleChoices.loadedAt < 60000)) return;
  bundleChoices.loading = true;
  let found = null, failure = null;
  try { found = await api(`/api/v1/artifacts?profile=${encodeURIComponent(profile)}&limit=${BUNDLE_CHOICES}`); }
  catch (error) { failure = error.message; }
  // The operator may have picked another profile while this loaded.
  if (bundleChoices.profile !== profile) return;
  // Only what the service would flash: a manifest naming its commit, built
  // against the HIL agent the farm runs now. One built for an older agent is
  // refused at submit, so offering it would only produce the refusal.
  const listed = (found?.bundles || []).filter(bundle => bundle.manifest_valid && bundle.revision);
  Object.assign(bundleChoices, {
    bundles: listed.filter(bundle => bundle.agent_current !== false),
    stale: listed.filter(bundle => bundle.agent_current === false).length,
    listed: (found?.bundles || []).length,
    matched: found?.matched ?? 0, error: failure, loading: false, loadedAt: Date.now(),
  });
  renderBundleChoices();
}

function bundleChoiceLabel(bundle) {
  const families = (bundle.families || []).length;
  return [
    bundle.revision.slice(0, 10), bundle.branch || "no branch", bundle.actor ? `@${bundle.actor}` : "",
    relativeWhen(bundle.created_at), `${families} famil${families === 1 ? "y" : "ies"}`, bundle.pinned ? "pinned" : "",
  ].filter(Boolean).join(" · ");
}

function renderBundleChoices() {
  const select = $("bundle-select");
  const profile = $("profile-select").value;
  if (!select || bundleChoices.profile !== profile) return;
  const spec = profiles[profile] || {};
  let bundles = bundleChoices.bundles;
  if (preferredBundle?.profile === profile && !bundles.some(bundle => bundle.id === preferredBundle.id)) bundles = [preferredBundle, ...bundles];
  const wanted = preferredBundle?.profile === profile ? preferredBundle.id : select.value;
  const chosen = bundles.some(bundle => bundle.id === wanted) ? wanted : bundles[0]?.id || "";
  bundleChoices.shown = bundles;
  const producer = spec.supply_workflow
    ? `<code>${escapeHtml(spec.supply_workflow)}</code>${spec.supply_repo ? ` in ${repoLink(spec.supply_repo)}` : ""}`
    : "its project's CI";
  const signature = JSON.stringify([profile, bundles.map(bundleChoiceLabel), bundles.map(bundle => bundle.id), chosen, bundleChoices.loading, bundleChoices.error, bundleChoices.matched, bundleChoices.stale]);
  if (signature !== bundleChoices.signature) {
    bundleChoices.signature = signature;
    select.innerHTML = bundles.length
      ? bundles.map(bundle => `<option value="${escapeHtml(bundle.id)}"${bundle.id === chosen ? " selected" : ""}>${escapeHtml(bundleChoiceLabel(bundle))}</option>`).join("")
      : `<option value="">${bundleChoices.loading || !bundleChoices.loadedAt ? `Loading the bundles ${site()} holds…` : "No bundle held for this project"}</option>`;
    select.disabled = !bundles.length;
    $("suite-submit").disabled = !bundles.length;
    const hint = [
      bundleChoices.matched > bundleChoices.listed ? `newest ${bundleChoices.listed} of ${bundleChoices.matched}` : "",
      bundleChoices.stale ? `${bundleChoices.stale} built for an older HIL agent not offered` : "",
    ].filter(Boolean).join(" · ");
    $("bundle-hint").textContent = hint ? `(${hint})` : "";
    // Saying where firmware comes from is the difference between an operator
    // understanding an empty list and reading it as the farm being broken.
    $("bundle-note").innerHTML = bundleChoices.error
      ? `<span class="failure-summary">Could not list the bundles: ${escapeHtml(bundleChoices.error)}</span>`
      : bundles.length || !bundleChoices.loadedAt
        ? `${Site()} does not build firmware: it flashes bundles built by ${producer}. To test another commit, run that workflow. <a href="#artifacts">Every bundle</a>`
        : `${Site()} holds no ${escapeHtml(spec.label || profile)} bundle, and it does not build firmware. Run ${producer}: it builds the bundle and dispatches the run that flashes it.`;
  }
  renderFamilies(familyArgs.targets, familyArgs.inv);
}

function selectedBundle() {
  const id = $("bundle-select")?.value;
  return (bundleChoices.shown || []).find(bundle => bundle.id === id) || null;
}

// From a bundle's page: the run form, on that bundle's profile, with it chosen.
function runWithBundle(entry) {
  preferredBundle = entry;
  const select = $("profile-select");
  if ([...select.options].some(option => option.value === entry.profile)) select.value = entry.profile;
  navigateTo("#runs");
  $("run-card").open = true;
  renderProfiles();
  renderBundleChoices();
  loadBundleChoices(entry.profile, {force: true});
  bringIntoView($("run-card"));
}

let familyArgs = {targets: [], inv: {}};
function renderFamilies(targets, inv) {
  familyArgs = {targets, inv};
  const connected = new Set((inv.boards || []).map(board => board.target));
  // A run can only ask for images its bundle carries; the rest are offered
  // disabled, so the choice explains itself instead of being refused.
  const bundle = selectedBundle();
  const carried = bundle ? new Set((bundle.families || []).map(family => family.family)) : null;
  // The project decides which boards a run takes: with `needs`, exactly
  // those families and no other are asked of the bundle; without, the run
  // takes the bench and every connected family is flashed.
  const project = profiles[$("profile-select")?.value] || null;
  const needed = project && (project.needs || []).length ? new Set(project.needs.map(need => need.target)) : null;
  const signature = JSON.stringify([targets, [...connected].sort(), carried && [...carried].sort(), needed && [...needed].sort()]);
  if (signature === familySignature) return;
  const fieldset = $("artifact-families");
  const previous = new Map([...fieldset.querySelectorAll("input[name=target]:not(:disabled)")].map(input => [input.value, input.checked]));
  familySignature = signature;
  const boxes = targets.map(target => {
    const absent = carried && !carried.has(target);
    const unneeded = needed && !needed.has(target);
    const checked = !absent && !unneeded && (needed ? true : previous.has(target) ? previous.get(target) : connected.has(target));
    const count = (inv.boards || []).filter(board => board.target === target).length;
    return `<label title="${escapeHtml(target)} · ${absent ? "not in this bundle" : unneeded ? "not a family this project takes" : `${count} connected board(s)`}"${absent || unneeded ? ' class="muted"' : ""}><input type="checkbox" name="target" value="${escapeHtml(target)}"${checked ? " checked" : ""}${absent || unneeded ? " disabled" : ""}> ${escapeHtml(familyLabel(target))}${count ? ` <small>×${count}</small>` : " <small class=muted>none</small>"}</label>`;
  });
  fieldset.innerHTML = `<legend>Artifact families${needed ? ' <small class="muted">the project\'s: ' + escapeHtml([...needed].join(", ")) + "</small>" : ""}</legend>${boxes.join("") || "<span class=muted>The service reported no artifact families.</span>"}`;
}

// ---- Test selection ----------------------------------------------------------
// The suite's files, read from the service, so a test added to the suite
// shows up here without a page change. Nothing checked means the whole
// suite; the note on the summary says which. Rebuilt only when the
// catalogue changes, so the operator's ticks survive the polling refresh.
let suiteSignature = "";
function renderSuiteTests(catalogue) {
  const signature = JSON.stringify(catalogue);
  if (signature === suiteSignature) return;
  suiteSignature = signature;
  const fieldset = $("test-files");
  const previous = new Set([...fieldset.querySelectorAll("input[name=test]:checked")].map(input => input.value));
  fieldset.innerHTML = `<legend>Test files</legend>${(catalogue || []).map(entry => {
    const tests = entry.tests || [];
    const capabilities = [...new Set(tests.flatMap(test => test.capabilities || []))];
    return `<label class="test-file" title="${escapeHtml(tests.map(test => test.name).join("\n"))}"><input type="checkbox" name="test" value="${escapeHtml(entry.file)}"${previous.has(entry.file) ? " checked" : ""}> ${escapeHtml(entry.file.replace(/^test_/, "").replace(/\.py$/, "").replaceAll("_", " "))} <small>${escapeHtml(tests.length)} test${tests.length === 1 ? "" : "s"}${capabilities.length ? ` · ${escapeHtml(capabilities.join(", "))}` : ""}</small></label>`;
  }).join("") || `<span class=muted>${profiles[$("profile-select")?.value]?.location === "consumer" ? "This project's suite is checked out per run, so its files are not listed here: name a test file or a keyword below, or run the whole suite." : "The service reported no suite tests."}</span>`}`;
  fieldset.querySelectorAll("input[name=test]").forEach(input => input.addEventListener("change", updateSelectionNote));
  updateSelectionNote();
}

function updateSelectionNote() {
  const files = [...document.querySelectorAll("#test-files input[name=test]:checked")].length;
  const keyword = $("suite-form").elements.keyword?.value.trim();
  $("test-selection-note").textContent = files || keyword ? `partial: ${files ? `${files} file${files === 1 ? "" : "s"}` : "all files"}${keyword ? ` matching "${keyword}"` : ""}` : "whole suite";
}

// What a run asked for: the selection from its request, and the failed
// tests from its report — both as the file-or-test names the service takes.
function selectionOf(job) {
  return {tests: job.request?.tests || [], keyword: job.request?.keyword || ""};
}
function failedTestsOf(job) {
  const ids = Object.values(job.report?.capabilities || {}).filter(item => item.status !== "validated").flatMap(item => item.tests || []);
  // Test ids arrive prefixed with the running profile's suite path; strip
  // whichever one it is rather than assuming painlessMesh's.
  const prefixes = Object.values(profiles).map(p => p.suite_path).filter(Boolean);
  const strip = id => prefixes.reduce((acc, dir) => acc.startsWith(dir + "/") ? acc.slice(dir.length + 1) : acc, id);
  return [...new Set(ids.map(id => strip(id).replace(/\[.*$/, "")).filter(id => /^test_[a-z0-9_]+\.py(::test_[A-Za-z0-9_]+)?$/.test(id)))];
}
async function rerun(job, tests, keyword = "") {
  const body = {profile: jobProfile(job), ref: job.request?.resolved_sha || job.request?.ref, targets: job.request?.targets || undefined, reuse: true};
  if (tests.length) body.tests = tests;
  if (keyword) body.keyword = keyword;
  const queued = await api("/api/v1/suites", {method: "POST", body: JSON.stringify(body)});
  await refresh(true); await showJob(queued.id, {focus: true, force: true});
}

// ---- Hardware -------------------------------------------------------------
// What the silicon said the last time discovery read it, kept by the service
// keyed by MAC and delivered with the inventory. A refresh re-reads one board
// on demand; nothing here opens a port while a run holds the rig.
const DETAIL_ROWS = [
  ["description", "Chip"],
  ["revision", "Silicon revision"],
  ["crystal", "Crystal"],
  ["flash_size", "Flash size"],
  ["flash_type", "Flash type"],
  ["transport", "Console transport"],
  ["usb_mode", "USB mode"],
  ["port", "Serial port"],
  ["usb_path", "USB path"],
  ["usb_serial", "USB serial"],
  ["esptool_version", "Read by esptool"],
];

// Transient state of an on-demand re-read, keyed by board id.
const detailReads = new Map();

function renderDetails(board) {
  const read = detailReads.get(board.id);
  if (read?.state === "loading") return `<p class="muted">Reading the chip over its serial port…</p>`;
  const device = board.details;
  const error = read?.state === "error" ? `<p class="failure-summary">${escapeHtml(read.message)}</p>` : "";
  if (!device) return `${error}<p class="muted">No chip reading on file yet. Discovery reads every board; a rediscover, or a run, fills this in.</p>`;
  const rows = DETAIL_ROWS.filter(([key]) => device[key]).map(([key, label]) => `<div><small>${escapeHtml(label)}</small><span>${escapeHtml(device[key])}</span></div>`);
  if (device.flash_manufacturer) rows.push(`<div><small>Flash chip</small><span>manufacturer ${escapeHtml(device.flash_manufacturer)} · device ${escapeHtml(device.flash_device || "?")}</span></div>`);
  // A MAC that disagrees with the registry means the part on this port is not
  // the one registered here. That is the finding, so it leads.
  const identity = device.matches_registry
    ? ""
    : `<p class="failure-summary">This port answered with MAC ${escapeHtml(device.mac)}, but ${escapeHtml(board.id)} is registered as ${escapeHtml(device.registered_mac || "no MAC")}. The board map is stale — rediscover.</p>`;
  const features = (device.features || []).map(item => `<span class="artifact">${escapeHtml(item)}</span>`).join("");
  return `${error}${identity}<div class="detail-grid">${rows.join("")}</div>${features ? `<div class="artifacts"><strong>Features</strong>${features}</div>` : ""}<small class="muted">Read ${escapeHtml(device.probed_at ? new Date(device.probed_at).toLocaleString() : "at an unknown time")}</small>`;
}

async function loadDetails(boardId) {
  detailReads.set(boardId, {state: "loading"});
  renderBoard();
  try {
    await api(`/api/v1/inventory/${encodeURIComponent(boardId)}/details`);
    detailReads.delete(boardId);
    await refresh(true);
  } catch (error) {
    detailReads.set(boardId, {state: "error", message: error.message});
    renderBoard();
  }
}

// "Connected" says a board is plugged in; it does not say whether it is free.
// A board driving a validation run is both connected and unavailable, and
// anything that opens its serial port meanwhile — a chip-details probe, say —
// takes it away from the run. So the state is what is shown, and the probe is
// refused at the button rather than at the 409 that would come back anyway.
function boardState(board) {
  if (board.state === "in_use") return {label: "IN USE", tone: "warn", free: false};
  // Held out of the pool: nothing is allocated it, but nothing holds its
  // serial port either, so its chip can still be read.
  if (board.state === "reserved") return {label: "RESERVED", tone: "warn", free: true};
  if (board.state === "quarantined") return {label: "QUARANTINED", tone: "bad", free: true};
  return {label: "AVAILABLE", tone: "good", free: true};
}

// Why a board is out of the pool, and the control that puts it back or takes
// it out: reserved for bench work by an operator, or quarantined by the canary
// for failing its own checks run after run.
function holdFacts(board) {
  const hold = board.hold;
  const control = hold
    ? `<button class="board-release secondary admin-only" data-id="${escapeHtml(board.id)}" title="Put this board back in the pool">Release</button>`
    : `<button class="board-reserve secondary admin-only" data-id="${escapeHtml(board.id)}" title="Take this board out of the pool for bench work, without unregistering it">Reserve</button>`;
  const note = hold
    ? `<small class="hold-note">${hold.state === "quarantined" ? "Quarantined" : "Reserved"}${hold.by ? ` by ${escapeHtml(hold.by)}` : ""} ${escapeHtml(new Date(hold.since).toLocaleString())}${hold.reason ? `: ${escapeHtml(hold.reason)}` : ""}${hold.state === "quarantined" ? ". A clean health check releases it." : ""}</small>`
    : "";
  return {note, control};
}

function chipFacts(board) {
  const d = board.details;
  if (!d) return "";
  const facts = [d.description && `<b>${escapeHtml(d.description)}</b>`, d.revision && `rev ${escapeHtml(d.revision)}`, d.flash_size && `${escapeHtml(d.flash_size)} flash`, d.crystal && `${escapeHtml(d.crystal)} crystal`, d.transport && escapeHtml(d.transport)].filter(Boolean);
  return facts.length ? `<div class="chip-facts">${facts.map(f => `<span>${f}</span>`).join("")}</div>` : "";
}

// What the canary last said about this board. A board that started failing
// its radio join is visible here before it fails somebody's run -- and a
// failure the whole rig shares is labelled as the farm's, not this board's,
// because six boards failing one check is one fault, not six.
// Whether this farm has a canary at all: a profile like any other, so the
// service already says. A farm without one shows no health actions rather
// than buttons that cannot work.
function canaryAvailable() {
  return Boolean(profiles && profiles.canary);
}

function healthBadge(board) {
  const health = board.health;
  if (!health) return `<span class="state muted" title="The Rig Health Check has not checked this board yet">UNCHECKED</span>`;
  const when = health.checked_at ? `, ${shortWhen(health.checked_at)}` : "";
  if (health.verdict === "passed") {
    return `<span class="state good" title="Passed every Rig Health Check test${escapeHtml(when)}">HEALTHY</span>`;
  }
  const farmWide = (health.farm_wide || []).length;
  const failed = (health.failed || []).length;
  const label = farmWide && farmWide === failed ? "FARM FAULT" : "UNHEALTHY";
  return `<span class="state bad" title="${escapeHtml(checkNames(health.failed))} failed${escapeHtml(when)}">${label}</span>`;
}

function checkNames(checks) {
  return (checks || []).map(name => name.replace(/^test_/, "").replace(/_/g, " ")).join(", ");
}

function healthFacts(board) {
  const health = board.health;
  if (!health) return "";
  const failed = health.failed || [];
  const farmWide = health.farm_wide || [];
  const parts = [
    `health check ${escapeHtml(health.verdict)}${health.checked_at ? ` ${escapeHtml(shortWhen(health.checked_at))}` : ""}`,
  ];
  if (failed.length) {
    parts.push(`failed: ${escapeHtml(checkNames(failed))}`);
    // Same check red on every board: the rig, not this board. Said here so
    // nobody replaces a board over a broker that is down.
    if (farmWide.length) parts.push(`<b>${escapeHtml(checkNames(farmWide))} failed on every board: the farm, not this board</b>`);
  }
  if (health.job_id) parts.push(`<a href="#run/${escapeHtml(health.job_id)}">the run</a>`);
  return `<div class="chip-facts health">${parts.map(part => `<span>${part}</span>`).join("")}</div>`;
}


// The fleet by family: how many of each chip the farm has, how many are
// free, and what the silicon is — the overview an operator planning a run
// or a purchase wants, and the one the flat board list never gave.
function renderHardwareOverview(inv) {
  const boards = inv.boards || [];
  const missing = inv.missing || [];
  const families = new Map();
  for (const board of boards) {
    const entry = families.get(board.target) || {target: board.target, connected: 0, free: 0, held: 0, chips: new Set(), flash: new Set(), revisions: new Set()};
    entry.connected++;
    // Free is what a run could be given: not in use, and not held out of the
    // pool (reserved, quarantined).
    if (board.state === "available") entry.free++;
    else if (board.state !== "in_use") entry.held++;
    const d = board.details || {};
    if (d.description) entry.chips.add(d.description); else if (board.chip) entry.chips.add(board.chip);
    if (d.flash_size) entry.flash.add(d.flash_size);
    if (d.revision) entry.revisions.add(d.revision);
    families.set(board.target, entry);
  }
  // A missing board is known by its id only; its family is the id's prefix
  // when the operator named it that way, which the register form suggests.
  for (const id of missing) {
    const target = (inv.targets || []).find(t => id.startsWith(`${t}-`)) || id.replace(/-[^-]*$/, "");
    const entry = families.get(target) || {target, connected: 0, free: 0, held: 0, chips: new Set(), flash: new Set(), revisions: new Set()};
    entry.missing = (entry.missing || 0) + 1;
    families.set(target, entry);
  }
  const rows = [...families.values()].sort((a, b) => a.target.localeCompare(b.target)).map(f => {
    const chips = [...f.chips].join(", ") || "not read yet";
    const facts = [f.flash.size ? `${[...f.flash].join("/")} flash` : "", f.revisions.size ? `rev ${[...f.revisions].join(", ")}` : ""].filter(Boolean).join(" · ");
    const busy = f.connected - f.free - f.held;
    return `<tr><td><strong>${escapeHtml(familyLabel(f.target))}</strong><small>${escapeHtml(f.target)}</small></td><td>${escapeHtml(chips)}${facts ? `<small>${escapeHtml(facts)}</small>` : ""}</td><td class="num"><strong>${f.connected}</strong></td><td class="num ${f.free ? "good" : "muted"}">${f.free}${f.held ? `<small>${f.held} held out</small>` : ""}</td><td class="num ${busy ? "warn" : "muted"}">${busy}</td><td class="num ${f.missing ? "warn" : "muted"}">${f.missing || 0}</td></tr>`;
  });
  const free = boards.filter(b => b.state === "available").length;
  const inUse = boards.filter(b => b.state === "in_use").length;
  const heldOut = boards.length - free - inUse;
  $("hardware-overview").innerHTML = rows.length
    ? `<div class="hw-total"><span><strong>${boards.length}</strong> boards connected</span><span><strong>${families.size}</strong> families</span><span><strong>${free}</strong> available</span><span><strong>${inUse}</strong> in use</span>${heldOut ? `<span class="warn"><strong>${heldOut}</strong> held out</span>` : ""}${missing.length ? `<span class="warn"><strong>${missing.length}</strong> missing</span>` : ""}</div>
       <div class="table-wrap"><table class="hw-table"><thead><tr><th>Family</th><th>Silicon</th><th class="num">Boards</th><th class="num">Free</th><th class="num">In use</th><th class="num">Missing</th></tr></thead><tbody>${rows.join("")}</tbody></table></div>`
    : "<p class=muted>No boards connected.</p>";
}


// Which revision of this farm is actually running, stamped into the install
// rather than read from a clone that may have moved on. An operator reading a
// run report needs to know what produced it — and where to read the code.
// ---- updates, on the owner's terms ---------------------------------------------------
function updateStateLine(view) {
  const status = view.status || {};
  const tone = status.state === "failed" ? "bad" : UPDATING.includes(status.state) ? "warn" : status.state === "installed" ? "good" : "muted";
  const words = {
    downloading: `Downloading ${status.version || "the release"}${status.detail ? ` — ${status.detail}` : ""}`,
    staged: `${status.version || "The release"} is staged; the update unit is installing it`,
    installing: `Installing ${status.version || "the release"}: the service restarts when it is done`,
    installed: `${status.version || "The release"} installed ${status.at ? relativeWhen(status.at) : ""}`,
    failed: `The last install failed${status.detail ? `: ${status.detail}` : ""}`,
    available: status.detail || `${status.version || "A release"} is available`,
    pending: status.detail || "Waiting for the rig to be idle",
  }[status.state];
  return words ? `<p class="${tone === "bad" ? "failure-summary" : tone === "muted" ? "muted" : ""} update-state">${escapeHtml(words)}</p>` : "";
}

function renderUpdate(view) {
  const block = $("update-block");
  if (!block || !view) return;
  lastUpdateView = view;
  const busy = view.staging || UPDATING.includes(view.status?.state);
  const available = view.available;
  const source = view.source === "portal" ? "the farm this rig is a node of" : "GitHub, the rig software's releases";
  const newest = available
    ? `<span class="state warn">${escapeHtml(available.version || "a newer release")}</span>${available.published_at ? ` <small class="muted">published ${escapeHtml(relativeWhen(available.published_at))}</small>` : ""}${available.html_url ? ` <a href="${escapeHtml(available.html_url)}" target="_blank" rel="noopener">release notes</a>` : ""}`
    : view.error
      ? `<span class="warn">${escapeHtml(view.error)}</span>`
      : view.checked_at || view.source === "portal"
        ? `<span class="state good">up to date</span>${view.checked_at ? ` <small class="muted">checked ${escapeHtml(relativeWhen(view.checked_at))}</small>` : ""}`
        : '<span class="muted">not checked yet</span>';
  const facts = [
    ["Installed", `<strong>${escapeHtml(view.installed?.version || "unknown")}</strong>`],
    ["Newest", newest],
    ["From", escapeHtml(source)],
  ];
  const admin = isAdmin() && shell().projectsAreOwn;
  block.innerHTML = `<div class="live-facts worker-facts update-facts">${facts.map(([label, value]) => `<div><small>${label}</small><span>${value}</span></div>`).join("")}</div>
    ${updateStateLine(view)}
    ${admin ? `<div class="row-actions update-actions">
      <button type="button" class="secondary update-check"${busy ? " disabled" : ""}>Check for updates</button>
      ${available && !busy ? `<button type="button" class="update-install">Install ${escapeHtml(available.version || "it")}</button>` : ""}
      <label class="check"><input type="checkbox" class="update-auto"${view.auto ? " checked" : ""}> Install updates automatically <small class="muted">when the rig is idle; the install restarts the service</small></label>
    </div>` : ""}`;
  block.querySelector(".update-check")?.addEventListener("click", async event => {
    event.currentTarget.disabled = true;
    try { renderUpdate((await api("/api/v1/update/check", {method: "POST", body: "{}"})).update); }
    catch (error) { alert(`This rig refused: ${error.message}`); loadUpdate(true); }
  });
  block.querySelector(".update-install")?.addEventListener("click", async event => {
    if (!confirm(`Install ${available?.version || "the newer release"} now? The service restarts when it is done; a run in progress would end.`)) return;
    event.currentTarget.disabled = true;
    try { renderUpdate((await api("/api/v1/update/install", {method: "POST", body: "{}"})).update); }
    catch (error) { alert(`This rig refused: ${error.message}`); loadUpdate(true); }
  });
  block.querySelector(".update-auto")?.addEventListener("change", async event => {
    try { renderUpdate((await api("/api/v1/update/auto", {method: "POST", body: JSON.stringify({auto: event.target.checked})})).update); }
    catch (error) { alert(`This rig refused: ${error.message}`); loadUpdate(true); }
  });
  // While something is being fetched or installed, follow it.
  clearTimeout(updateTimer);
  if (busy) updateTimer = setTimeout(() => loadUpdate(true), 4000);
}

async function loadUpdate(force = false) {
  if (!shell().projectsAreOwn || !$("update-block")) return;
  if (!force && Date.now() - updateLoadedAt < 60000) return;
  try {
    renderUpdate(await api("/api/v1/update"));
    updateLoadedAt = Date.now();   // only a load that worked holds the next one back
  } catch { /* a key that may not read it, or a rig before updates: the card stays as it was, and the next status poll tries again */ }
}

function renderVersion(version, repos) {
  const number = version?.version || "unknown";
  const known = number !== "unknown";
  const pill = $("version");
  pill.textContent = known ? `v${number}` : "version unknown";
  pill.className = known ? "version" : "version warn";
  pill.title = known
    ? `${number} · deployed ${version.installed_at || "at an unknown time"}${version.subject ? ` · ${version.subject}` : ""}`
    : "No version stamp: this host was provisioned before versions were recorded, or by hand.";
  const commitUrl = repos.farm && version?.commit ? `${repos.farm}/commit/${version.commit}` : null;
  if (commitUrl) pill.href = commitUrl; else pill.removeAttribute("href");
  const link = $("repo-link");
  link.hidden = !repos.farm;
  if (repos.farm) { link.href = repos.farm; $("repo-name").textContent = repos.farm.replace(/^https:\/\/github\.com\//, ""); }
  // What this rig is for: its projects' repositories (the health check is
  // not one). A portal says what its default profile validates.
  const projectNames = shell().projectsAreOwn
    ? Object.keys(profiles).filter(name => name !== "canary").sort()
    : [defaultProfile];
  const underTest = shell().projectsAreOwn
    ? projectNames.map(repoForProfile).filter(Boolean)
    : [repoForProfile(defaultProfile)].filter(Boolean);
  const rows = [
    ["Version", number],
    ["Build", version?.build],
    ["Commit", version?.commit],
    ["Committed", version?.committed_at],
    ["Deployed", version?.installed_at],
  ].filter(([, value]) => value !== undefined && value !== null && value !== "");
  $("version-detail").innerHTML = known
    ? `<div class="detail-grid">${rows.map(([label, value]) => `<div><small>${escapeHtml(label)}</small><span>${label === "Commit" ? shaLink(value, repos.farm) : escapeHtml(value)}</span></div>`).join("")}</div>${version.subject ? `<p class="muted">${escapeHtml(version.subject)}</p>` : ""}${repos.farm ? `<p class="muted">${shell().projectsAreOwn ? "Rig software" : "Source"}: <a href="${escapeHtml(repos.farm)}" target="_blank" rel="noopener">${escapeHtml(repos.farm)}</a>${underTest.length ? ` · ${shell().projectsAreOwn ? "projects" : "under validation"}: ${underTest.map(url => `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(url.replace(/^https:\/\/github\.com\//, ""))}</a>`).join(", ")}` : shell().projectsAreOwn ? ' · <a href="#configuration/projects">no project yet</a>' : ""}</p>` : ""}`
    : `<p class="muted">This host reports no version stamp. It is running code installed before the farm recorded one, or installed by hand; re-run <code>install-health-service.sh</code> (the deploy workflow does) to stamp it.</p>`;
  loadUpdate();
}

// ---- Workers ------------------------------------------------------------------
// A portal's nodes: whether each is there, what it runs, and where it is with
// the release the portal names -- the things "is the farm up" now depends on,
// which a page built for one Pi had nowhere to show.
function farmCommitLink(sha) { return sha ? shaLink(sha, repositories.farm) : '<span class="muted">unknown</span>'; }

// A release by its number -- 1.0.281 -- as rigs report theirs; the commit is
// provenance, shown beside it where there is room.
function releaseName(commit) {
  if (!commit) return "unknown";
  return currentRelease?.commit === commit && currentRelease.version ? currentRelease.version : String(commit).slice(0, 9);
}

function releaseState(worker, current = currentRelease?.commit) {
  const update = worker.update || {};
  const target = currentRelease?.version || String(current || "").slice(0, 9);
  if (update.state === "available") return {tone: "warn", label: "update available", detail: update.detail || `${target} is available; its owner installs it, or turns automatic installs on.`};
  if (UPDATING.includes(update.state)) return {tone: "warn", label: `updating · ${update.state}`, detail: update.detail || `to ${releaseName(update.commit)}`};
  if (update.state === "failed" && (!current || update.commit === current)) return {tone: "bad", label: "update failed", detail: update.detail || "the install failed"};
  if (!current) return {tone: "muted", label: "no release published", detail: ""};
  if (worker.commit === current) return {tone: "good", label: "current", detail: ""};
  return {tone: "warn", label: "behind", detail: `Runs ${worker.version || String(worker.commit || "an unknown release").slice(0, 9)}; the current release is ${target}.`};
}

function workerState(worker) {
  if (!worker.online) return {tone: "bad", label: "OFFLINE"};
  if (worker.drained) return {tone: "warn", label: "DRAINED"};
  if (UPDATING.includes(worker.update?.state)) return {tone: "warn", label: "UPDATING"};
  if (worker.running) return {tone: "warn", label: "RUNNING"};
  return {tone: "good", label: "ONLINE"};
}

function workerHealth(worker) {
  return (worker.health && typeof worker.health === "object" ? worker.health.status : worker.health) || "unknown";
}

function workerHeading(worker) {
  const state = workerState(worker);
  return `<div class="title-row"><div><p class="eyebrow">${worker.kind === "hardware" ? "HARDWARE NODE" : `${escapeHtml(String(worker.kind).toUpperCase())} WORKER`}</p><h2>${escapeHtml(worker.name)}</h2></div><span class="state ${state.tone}">${state.label}</span></div>`;
}

// The rig's GitHub in one phrase: what its own page says (a local rig), or
// what it reported to the portal (a rig there). A rig on a release before
// this one reports nothing, and the fact says so rather than guessing.
function githubFact(github) {
  if (!github) return '<span class="muted">not reported</span>';
  if (!github.configured) return '<span class="state warn">not connected</span>';
  if (!github.connected) return '<span class="state bad">refused</span>';
  const days = github.expires_in_days;
  const soon = typeof days === "number" && days <= 14;
  return `<span class="state good">connected</span> ${escapeHtml(github.login || "")}${github.kind ? ` <small class="muted">${escapeHtml(github.kind)}</small>` : ""}${typeof days === "number" ? ` <small class="${soon ? "warn" : "muted"}">${days < 0 ? "expired" : `expires in ${days} d`}</small>` : ""}`;
}

function workerFacts(worker, {long = false} = {}) {
  const release = releaseState(worker);
  const health = workerHealth(worker);
  // A rig on its own page says what it has. A release, when it was last
  // heard, whose it is and who may see it are a portal's facts about one of
  // its rigs; here they would be "unknown", "just now", "the farm" and
  // "private" -- true of nothing.
  const local = Boolean(worker.local);
  const facts = [
    local
      ? ["Version", `<strong>${escapeHtml(worker.version || "unknown")}</strong>`]
      : ["Release", `<strong>${escapeHtml(worker.version || "unknown")}</strong> <small class="state ${release.tone}">${escapeHtml(release.label)}</small>`],
    ["Runs", `${escapeHtml(worker.running)} of ${escapeHtml(worker.max_runs)}`],
    ["Boards", `${escapeHtml(worker.boards)}${worker.missing ? ` <small class="warn">${escapeHtml(worker.missing)} missing</small>` : ""}`],
    ["Host health", `<span class="${health === "ok" ? "good" : health === "unknown" ? "muted" : "warn"}">${escapeHtml(health)}</span>`],
    ...(local ? [] : [["Last heard", whenSpan(worker.seen_at)]]),
  ];
  if (long && local) {
    facts.push(
      ["Location", worker.location ? escapeHtml(worker.location) : '<span class="muted">not set</span>'],
      ["GitHub", githubFact(worker.github || lastGithubSummary)],
      ["Commit", farmCommitLink(worker.commit)],
      ["Projects", escapeHtml((worker.profiles || []).filter(name => name !== "canary").join(", ") || "none yet")]);
  } else if (long) {
    facts.push(
      // Whose rig it is: the person who administers it, and what its own
      // settings and its events belong to.
      // A rig nobody owns is the farm's; a rig lent to this caller is
      // somebody's, and the handle is withheld rather than absent. Reading
      // the redaction as "unowned" told borrowers the opposite of the truth.
      ["Owner", worker.owner
        ? `${escapeHtml(worker.owner)}${worker.owner === you?.name ? " <small class=\"muted\">(you)</small>" : ""}`
        : worker.lent
          ? '<span class="muted">another workspace</span>'
          : '<span class="muted">the farm</span>',
       isAdmin() ? "An admin gives a rig to somebody; they administer it from then on." : ""],
      ["Visibility", visibilityFact(worker)],
      ["Location", worker.location ? escapeHtml(worker.location) : '<span class="muted">not set</span>'],
      ["GitHub", githubFact(worker.github)],
      ["Connected", whenSpan(worker.hello_at)], ["Commit", farmCommitLink(worker.commit)],
      ["Profiles", escapeHtml((worker.profiles || []).join(", ") || "—")]);
  }
  return `<div class="live-facts worker-facts">${facts.map(([label, value]) => `<div><small>${label}</small><span>${value}</span></div>`).join("")}</div>${release.detail ? `<p class="${release.tone === "bad" ? "failure-summary" : "muted"} worker-release">${escapeHtml(release.detail)}</p>` : ""}`;
}

// Who may see a rig. Private until its owner says otherwise; public puts it
// on the world page (/world) -- what it can do, whether it is up, how busy --
// for anyone, with no key; shared lets others run on it, which at launch is
// the farm's own rigs and an admin's to say. Its owner sees the buttons.
const VISIBILITY_SAID = {
  private: "private — its owner and the farm",
  public: "public — on the world page, for anyone",
  shared: "shared — on the world page, and others may run on it",
};

function visibilityFact(rig) {
  const current = rig.visibility || "private";
  const said = escapeHtml(VISIBILITY_SAID[current] || current);
  if (!ownsRig(rig)) return said;
  // A labelled control, not a badge with buttons: the levels to choose
  // from, and what the chosen one shows said beside it. Sharing is the
  // farm's to decide, so only an admin is offered it.
  const levels = [["private", "Private"], ["public", "Public"]];
  if ((isAdmin() && current !== "shared") || current === "shared") levels.push(["shared", "Shared"]);
  return `<label class="visibility-control"><select class="rig-visibility-select" aria-label="Who sees this rig">${levels.map(([value, label]) =>
    `<option value="${value}"${value === current ? " selected" : ""}>${label}</option>`).join("")}</select><small class="muted">${said}</small></label>`;
}

// Change who sees a rig, after saying what that means: its description and
// its location go on the world page as written on its page, so they are
// named, with what they say now -- a location is a fact about somebody's
// home. Never its keys, its addresses, its boards' identities or its runs.
async function changeVisibility(rig, wanted, control = null) {
  const name = rig.name;
  const written = [rig.description ? `its description ("${rig.description}")` : "",
                   rig.location ? `its location ("${rig.location}")` : ""].filter(Boolean).join(" and ");
  const asks = {
    public: `Make ${name} public? Anyone can then see its name${written ? `, ${written}` : ""}, what it can do, whether it is up and how busy it is — never its keys, its addresses, its boards' identities or its runs.`,
    shared: `Share ${name}? It is public — its name${written ? `, ${written}` : ""}, what it can do, whether it is up — and other accounts may run on it.`,
  };
  if (asks[wanted] && !confirm(asks[wanted])) {
    if (control) control.value = rig.visibility || "private";
    return;
  }
  try {
    await api(`/api/v1/rigs/${encodeURIComponent(name)}/visibility`,
              {method: "POST", body: JSON.stringify({visibility: wanted})});
  } catch (error) { alert(`Could not change who sees ${name}: ${error.message}`); }
  return refresh(true);
}

function renderNodeBanner() {
  const banner = $("node-banner");
  banner.hidden = farmMode !== "node";
  if (farmMode !== "node") return;
  const portal = portalUrl ? `<a href="${escapeHtml(portalUrl)}" target="_blank" rel="noopener">${escapeHtml(portalUrl)}</a>` : "its portal";
  banner.innerHTML = `<p class="eyebrow">NODE</p><h2>This rig takes its runs from ${portal}</h2><p class="muted">Runs are started, followed and reported on the portal, which also names the release this rig installs and is where it is managed. This page shows the boards and what ran on them here.</p>`;
}

// ---- Fleet: rigs, boards, releases -------------------------------------------
// A rig is a host with boards. On a portal every node is one, each managed
// from its page; on a farm host (standalone, or a node's own page) the host
// itself is the one rig. Boards have pages of their own too.
let rigsTab = "rigs";
// Rigs added on the portal that have not joined yet, as the status carries them.
let pendingRigs = [];
// A join command's token, by rig, from when it is made until it is used, runs
// out or the page is reloaded: shown once, kept nowhere else.
const joinTokens = new Map();
let lastStatus = null;
// `editing` is what the operator has open -- the rig's details or its
// settings -- which a poll must not re-render out from under them.
const rigPage = {name: null, detail: null, config: null, logs: null, runs: null, busy: new Set(), editing: null};
const boardPage = {id: null, history: null, rig: null};
const boardFilter = {q: "", rig: "", family: "", state: ""};
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

function localRig() {
  const inv = lastInventory || {};
  const status = lastStatus || {};
  return {
    name: "local", label: shell().localRigLabel, local: true, kind: "hardware", online: true,
    version: status.version?.version, commit: status.version?.commit,
    boards: (inv.boards || []).length, missing: (inv.missing || []).length,
    running: (lastQueue.running_jobs || []).length, max_runs: lastQueue.concurrency || 1,
    health: status.health?.status || "unknown", seen_at: new Date().toISOString(), hello_at: null,
    profiles: Object.keys(profiles), update: null, drained: null,
  };
}
function fleetRigs() { return shell().fleet(); }
function rigLabel(rig) { return rig.local ? rig.label : rig.name; }
function rigHref(name) { return name === "local" && shell().overviewIsRigPage ? "#overview" : `#rig/${encodeURIComponent(name)}`; }
function boardHref(id) { return `#board/${encodeURIComponent(id)}`; }
function boardsOf(rig) {
  const boards = (lastInventory || {}).boards || [];
  return rig.local ? boards : boards.filter(board => board.worker === rig.name);
}
function boardCounts(boards) {
  const available = boards.filter(board => board.state === "available").length;
  const inUse = boards.filter(board => board.state === "in_use").length;
  return {total: boards.length, available, inUse, held: boards.length - available - inUse};
}
function rigRelease(rig) {
  if (rig.local) return {tone: "muted", label: rig.version || "version unknown", detail: ""};
  return releaseState(rig);
}
function versionText(rig) {
  return rig.version ? `Release ${rig.version}` : "release unknown";
}

// Where a rig being added is: waiting for its join command to run, its token
// run out, or installing with the key it took.
function joinState(rig) {
  const status = rig.join?.status;
  if (status === "expired") return {tone: "bad", label: "TOKEN EXPIRED"};
  if (status === "installing") return {tone: "warn", label: "INSTALLING"};
  return {tone: "warn", label: "WAITING TO JOIN"};
}
// A section is re-rendered only when what it shows changed, and what the
// operator opened in it (a <details data-keep>) stays open. Every poll used to
// rewrite a rig's page whole: an open editor closed under the operator and the
// page jumped up as it shrank.
const renderedHtml = new WeakMap();
function renderSection(element, html) {
  if (!element || renderedHtml.get(element) === html) return false;
  const open = new Set([...element.querySelectorAll("details[data-keep][open]")].map(node => node.dataset.keep));
  element.innerHTML = html;
  renderedHtml.set(element, html);
  element.querySelectorAll("details[data-keep]").forEach(node => { if (open.has(node.dataset.keep)) node.open = true; });
  return true;
}
function forgetRendered(element) { renderedHtml.delete(element); }

function linkRows(root) {
  root.querySelectorAll("tr.clickable[data-href]").forEach(row => row.addEventListener("click", event => {
    if (event.target.closest("a,button,input,select,form,summary")) return;
    navigateTo(row.dataset.href);
  }));
}
function shortDetail(text, length = 160) {
  const value = String(text || "").replace(/\s+/g, " ").trim();
  return value.length > length ? `…${value.slice(-length)}` : value;
}

// ---- Overview ------------------------------------------------------------------
function renderOverview(data) {
  if (!$("rigs-online")) { renderRecentRuns(data.jobs || []); return; }   // a rig's document has no fleet overview
  const rigs = fleetRigs();
  const inv = lastInventory || {};
  const counts = boardCounts(inv.boards || []);
  const online = rigs.filter(rig => rig.online).length;
  const offline = rigs.length - online;
  const drained = rigs.filter(rig => rig.drained).length;
  const updating = rigs.filter(rig => UPDATING.includes(rig.update?.state)).length;
  $("rigs-online").textContent = shell().rigsOnlineText(online, rigs.length);
  $("rigs-note").textContent = [offline && `${offline} offline`, drained && `${drained} drained`, updating && `${updating} updating`].filter(Boolean).join(" · ")
    || shell().rigsIdleNote(rigs);
  $("connected").textContent = counts.available;
  const missing = (inv.missing || []).length;
  $("boards-note").textContent = `${counts.total} connected · ${counts.inUse} in use${counts.held ? ` · ${counts.held} held out` : ""}${missing ? ` · ${missing} missing` : ""}`;
  const jobs = data.jobs || [];
  const running = (lastQueue.running_jobs || []).map(id => jobs.find(job => job.id === id)).filter(Boolean);
  $("busy").textContent = running.length;
  const busyRigs = new Set(running.map(job => job.worker).filter(Boolean));
  $("running-note").textContent = running.length ? shell().runningNote(busyRigs) : "nothing running";
  const queued = lastQueue.queued || [];
  $("queued").textContent = queued.length;
  $("queue-note").textContent = lastQueue.paused ? "the queue is paused" : queued.length ? shortDetail((lastQueue.waiting || {})[queued[0]] || "waiting for a rig", 60) : "nothing waiting";
  renderOverviewRigs(rigs);
  renderAttention(rigs);
  renderRecentRuns(jobs);
}

function rigRow(rig) {
  const href = rigHref(rig.name);
  if (rig.pending) {
    const state = joinState(rig);
    return `<tr class="clickable" data-href="${href}">
    <td><a class="row-link" href="${href}">${escapeHtml(rig.name)}</a><small>${escapeHtml(rig.description || rig.location || "being added")}</small></td>
    <td><span class="state ${state.tone}">${state.label}</span></td>
    <td><span class="muted">—</span></td><td class="num muted">—</td><td class="num muted">—</td><td class="muted">—</td>
    <td class="nowrap">added ${whenSpan(rig.join?.created_at)}</td>
  </tr>`;
  }
  const state = workerState(rig);
  const release = rigRelease(rig);
  const boards = boardCounts(boardsOf(rig));
  const health = workerHealth(rig);
  const chips = capabilityChips(rig.setup);
  return `<tr class="clickable" data-href="${href}">
    <td><a class="row-link" href="${href}">${escapeHtml(rigLabel(rig))}</a><small>${escapeHtml([rig.description, rig.location].filter(Boolean).join(" · ") || (rig.local ? "this host" : ""))}</small>${chips ? `<div class="setup-chips compact">${chips}</div>` : ""}</td>
    <td><span class="state ${state.tone}">${state.label}</span>${rig.drained?.reason ? `<small>${escapeHtml(rig.drained.reason)}</small>` : ""}</td>
    <td><strong>${escapeHtml(rig.version || "unknown")}</strong>${rig.local ? "" : ` <span class="state ${release.tone}">${escapeHtml(release.label)}</span>`}</td>
    <td class="num"><strong>${boards.available}</strong> / ${boards.total}<small>${boards.inUse} in use${boards.held ? ` · ${boards.held} held` : ""}${rig.missing ? ` · ${rig.missing} missing` : ""}</small></td>
    <td class="num">${escapeHtml(rig.running)} of ${escapeHtml(rig.max_runs)}</td>
    <td><span class="${health === "ok" ? "good" : health === "unknown" ? "muted" : "warn"}">${escapeHtml(health)}</span></td>
    <td class="nowrap">${rig.local ? '<span class="muted">now</span>' : whenSpan(rig.seen_at)}</td>
  </tr>`;
}

function rigTable(rigs) {
  return `<div class="table-wrap"><table class="fleet"><thead><tr><th>Rig</th><th>State</th><th>Release</th><th class="num">Boards free</th><th class="num">Runs</th><th>Host</th><th>Last heard</th></tr></thead><tbody>${rigs.map(rigRow).join("")}</tbody></table></div>`;
}

function renderOverviewRigs(rigs) {
  const card = $("overview-rigs");
  const release = shell().releaseBadge();
  const {eyebrow, title, action} = shell().overviewRigs;
  card.innerHTML = `<div class="title-row"><div><p class="eyebrow">${eyebrow}</p><h2>${title}</h2></div><div class="row-actions">${release}${action}</div></div>${
    rigs.length ? rigTable(rigs) : '<p class="muted">No rig has connected to this portal yet. Add one from Rigs.</p>'}`;
  linkRows(card);
}

function attentionItems(rigs) {
  const items = [];
  const current = currentRelease?.commit;
  const inv = lastInventory || {};
  shell().attention(items, rigs);
  for (const rig of rigs) {
    const name = rigLabel(rig), href = rigHref(rig.name);
    if (!rig.online) items.push(["bad", `${name} is offline`, `Last heard ${relativeWhen(rig.seen_at)}; its boards are out of the pool.`, href]);
    if (rig.update?.state === "failed" && (!current || rig.update.commit === current)) items.push(["bad", `${name} could not install the current release`, shortDetail(rig.update.detail), href]);
    else if (UPDATING.includes(rig.update?.state)) items.push(["warn", `${name} is updating`, shortDetail(rig.update.detail || rig.update.state), href]);
    else if (!rig.local && rig.online && current && rig.commit !== current) items.push(["warn", `${name} is behind the current release`, `It runs ${rig.version || String(rig.commit || "an unknown release").slice(0, 9)}; the current release is ${currentRelease.version || current.slice(0, 9)}.`, href]);
    if (rig.drained) items.push(["warn", `${name} is drained`, rig.drained.reason || `By ${rig.drained.by || "an operator"}; it takes no new run.`, href]);
    const health = workerHealth(rig);
    if (rig.online && !["ok", "unknown"].includes(health)) items.push(["warn", `${name} reports its host ${health}`, "See its health checks.", href]);
  }
  for (const rig of pendingRigs) {
    const state = joinState(rig);
    items.push([state.label === "TOKEN EXPIRED" ? "warn" : "muted", `${rig.name} is being added: ${state.label.toLowerCase()}`, rig.join?.status === "expired" ? "Make a new join command on its page, or delete it." : rig.join?.status === "installing" ? `Installing${rig.join.hostname ? ` on ${rig.join.hostname}` : ""}; it joins when it says hello.` : "Run its join command on the new host.", rigHref(rig.name)]);
  }
  for (const board of inv.boards || []) {
    if (board.hold?.state === "quarantined") items.push(["bad", `${board.id} is quarantined`, shortDetail(board.hold.reason), boardHref(board.id)]);
    else if (board.health && board.health.verdict !== "passed") items.push(["bad", `${board.id} failed its Rig Health Check`, checkNames(board.health.failed), boardHref(board.id)]);
    if (board.hold?.state === "reserved") items.push(["muted", `${board.id} is reserved`, shortDetail(board.hold.reason || `by ${board.hold.by || "an operator"}`), boardHref(board.id)]);
  }
  for (const id of inv.missing || []) items.push(["warn", `${id} is missing`, "Registered, and not reported by its rig now.", boardHref(id)]);
  for (const device of inv.unregistered || []) items.push(["warn", `An unregistered ${device.target} is plugged in${device.worker ? ` on ${device.worker}` : ""}`, `${device.mac}: register it from its rig's page.`, device.worker ? rigHref(device.worker) : rigHref("local")]);
  for (const error of inv.probe_errors || []) items.push(["bad", `A port could not be read${error.worker ? ` on ${error.worker}` : ""}`, shortDetail(`${error.port}: ${error.error}`), error.worker ? rigHref(error.worker) : rigHref("local")]);
  if (lastQueue.paused) items.push(["warn", "The queue is paused", lastQueue.paused_reason || "Nothing queued will start.", "#overview"]);
  return items;
}

function renderAttention(rigs) {
  const items = attentionItems(rigs);
  const order = {bad: 0, warn: 1, muted: 2};
  items.sort((a, b) => order[a[0]] - order[b[0]]);
  $("overview-attention").innerHTML = `<div class="title-row"><div><p class="eyebrow">ATTENTION</p><h2>Needs attention</h2></div><span class="muted">${items.length ? `${items.length} item${items.length === 1 ? "" : "s"}` : ""}</span></div>${
    items.length
      ? `<ul class="attention-list">${items.slice(0, 10).map(([tone, title, detail, href]) => `<li><span class="dot ${tone}"></span><div><a href="${escapeHtml(href)}">${escapeHtml(title)}</a>${detail ? `<small>${escapeHtml(detail)}</small>` : ""}</div></li>`).join("")}</ul>${items.length > 10 ? `<p class="muted">+${items.length - 10} more</p>` : ""}`
      : `<p class="muted">Nothing needs attention: ${shell().allWellNote}every board is present and healthy.</p>`}`;
}

function renderRecentRuns(jobs) {
  if (!$("overview-recent")) return;
  const finished = jobs.filter(job => !["queued", "running"].includes(job.status)).slice(0, 8);
  $("overview-recent").innerHTML = `<div class="title-row"><div><p class="eyebrow">HISTORY</p><h2>Recent runs</h2></div><a class="button secondary" href="#runs">All runs</a></div>${
    finished.length
      ? `<div class="table-wrap"><table class="fleet"><thead><tr><th>Finished</th><th>Project</th><th>Rig</th><th>Status</th><th class="num">Took</th><th>Result</th></tr></thead><tbody>${finished.map(job => `<tr class="clickable" data-href="#run/${escapeHtml(job.id)}"><td class="nowrap">${whenSpan(job.finished_at || job.created_at)}</td><td><a class="row-link" href="#run/${escapeHtml(job.id)}">${escapeHtml(jobProject(job))}</a><small>${job.kind === "inventory" ? "discovery" : revisionLabel(job)}</small></td><td>${job.worker ? `<a href="${rigHref(job.worker)}">${escapeHtml(job.worker)}</a>` : '<span class="muted">—</span>'}</td><td><span class="state ${statusClass(job.status)}">${escapeHtml(job.status)}</span></td><td class="num">${escapeHtml(formatDuration(job.duration_seconds))}</td><td>${escapeHtml(shortDetail(jobSummary(job), 90))}</td></tr>`).join("")}</tbody></table></div>`
      : '<p class="muted">No run has finished yet.</p>'}`;
  linkRows($("overview-recent"));
}

// ---- Rigs page -------------------------------------------------------------------
function showRigsTab(tab, updateHash = true) {
  // Releases is the farm's own software, and `/api/v1/releases` is closed to
  // an account. The tab is hidden for them by CSS; this is the same rule for
  // an address, which the tab strip cannot cover.
  const offered = ["rigs", "boards", "releases"].filter(
    name => !(name === "releases" && workspaceOnly()));
  rigsTab = offered.includes(tab) ? tab : "rigs";
  $("rigs-list").hidden = rigsTab !== "rigs";
  if (rigsTab !== "rigs" && $("add-rig-card")) $("add-rig-card").hidden = true;
  $("boards-list").hidden = rigsTab !== "boards";
  $("releases").hidden = rigsTab !== "releases";
  document.querySelectorAll("#rigs-tabs a").forEach(link => link.classList.toggle("active", link.dataset.tab === rigsTab));
  if (updateHash) {
    history.replaceState(null, "", `#${rigsTab}`);
    lastRouted = location.hash;
  }
  if (rigsTab === "releases" && token) shell().releasesTab();
  renderFleet();
}

function renderFleet() {
  const rigs = fleetRigs();
  consoleRigs(rigs.map(rig => rig.name).filter(name => name && name !== "local"));
  const listed = shell().listedRigs(rigs, pendingRigs);
  const card = $("rigs-list");
  const adding = pendingRigs.length ? ` <small class="muted">${pendingRigs.length} being added</small>` : "";
  if (renderSection(card, `<div class="title-row"><div><p class="eyebrow">${shell().rigsEyebrow}</p><h2>${rigs.length} rig${rigs.length === 1 ? "" : "s"}${adding}</h2></div><span class="muted">${shell().releaseBadge().replace(/^<span class="muted">|<\/span>$/g, "")}</span></div>${
    listed.length ? rigTable(listed) : `<p class="muted">No rig has connected to this portal yet.${shell().noRigsHint}</p>`}`)) linkRows(card);
  renderBoardsTable();
  renderHardwareOverview(lastInventory || {});
  $("refresh").disabled = shell().rediscoverDisabled(rigBusy);
  $("refresh").textContent = shell().rediscoverLabel;
  // Nothing to rediscover: no button, and no note about one.
  const offered = shell().rediscoverOffered(rigs.length);
  $("refresh").hidden = !offered;
  $("refresh-note").hidden = !offered;
  // Adding a rig is what a portal is for, and it is everybody's: a person
  // brings their own, and it is theirs from the moment it is made. An
  // address with no account and no key is nobody yet.
  if ($("add-rig")) $("add-rig").hidden = !shell().addRig || !(isAdmin() || you?.account);
  const checkAll = $("check-all");
  checkAll.hidden = !canaryAvailable();
  checkAll.disabled = !((lastInventory || {}).boards || []).length;
  $("refresh-note").textContent = shell().rediscoverNote(rigBusy);
}

function renderBoardsTable() {
  const inv = lastInventory || {};
  const boards = inv.boards || [];
  const fill = (select, values, current) => {
    const signature = values.join("|");
    if (select.dataset.signature === signature) return;
    select.dataset.signature = signature;
    const first = select.options[0].outerHTML;
    select.innerHTML = first + values.map(value => `<option value="${escapeHtml(value)}"${value === current ? " selected" : ""}>${escapeHtml(value)}</option>`).join("");
  };
  fill($("board-rig"), [...new Set(boards.map(board => board.worker).filter(Boolean))].sort(), boardFilter.rig);
  $("board-rig").hidden = !shell().boardRigColumn;
  fill($("board-family"), [...new Set(boards.map(board => board.target))].sort(), boardFilter.family);
  const q = boardFilter.q.toLowerCase();
  const shown = boards.filter(board =>
    (!boardFilter.rig || board.worker === boardFilter.rig)
    && (!boardFilter.family || board.target === boardFilter.family)
    && (!boardFilter.state || board.state === boardFilter.state)
    && (!q || [board.id, board.mac, board.details?.description, board.worker].some(value => String(value || "").toLowerCase().includes(q))));
  const missing = (inv.missing || []).filter(id => !q || id.includes(q));
  const rows = shown.map(board => {
    const state = boardState(board);
    return `<tr class="clickable" data-href="${boardHref(board.id)}"><td><a class="row-link" href="${boardHref(board.id)}">${escapeHtml(board.id)}</a><small>${escapeHtml(board.mac || "")}</small></td>${shell().boardRigColumn ? `<td><a href="${rigHref(board.worker)}">${escapeHtml(board.worker || "—")}</a></td>` : ""}<td>${escapeHtml(familyLabel(board.target))}<small>${escapeHtml(board.details?.description || board.chip || "")}</small></td><td><span class="state ${state.tone}">${state.label}</span></td><td>${healthBadge(board)}<small>${board.health?.checked_at ? escapeHtml(shortWhen(board.health.checked_at)) : ""}</small></td><td>${escapeHtml(board.details?.flash_size || "")}<small>${escapeHtml(board.details?.transport || "")}</small></td></tr>`;
  });
  const missingRows = boardFilter.state ? [] : missing.map(id => `<tr class="clickable" data-href="${boardHref(id)}"><td><a class="row-link" href="${boardHref(id)}">${escapeHtml(id)}</a></td>${shell().boardRigColumn ? '<td><span class="muted">—</span></td>' : ""}<td><span class="muted">—</span></td><td><span class="state warn">MISSING</span></td><td></td><td></td></tr>`);
  $("boards-table").innerHTML = rows.length || missingRows.length
    ? `<div class="table-wrap"><table class="fleet"><thead><tr><th>Board</th>${shell().boardRigColumn ? "<th>Rig</th>" : ""}<th>Family · chip</th><th>State</th><th>Health check</th><th>Flash · console</th></tr></thead><tbody>${[...rows, ...missingRows].join("")}</tbody></table></div><p class="muted">${shown.length} of ${boards.length} boards${missing.length ? `, ${missing.length} missing` : ""}.</p>`
    : `<p class="muted">${boards.length ? "No board matches this filter." : "No board is connected."}</p>`;
  linkRows($("boards-table"));
}

// ---- A rig's page ------------------------------------------------------------------
async function showRig(name) {
  // Another rig is another rig's history and another rig's runs: both are
  // paged, and both start at the first page of the one being opened. The tab
  // is kept -- an operator reading activity across rigs stays in activity --
  // but what it holds is emptied, or rig A's commands are read under rig B's
  // name until the fetch lands.
  const arrived = rigPage.name !== name;
  if (arrived) {
    Object.assign(rigPage, {name, detail: null, config: null, logs: null, runs: null, editing: null,
                            runsOffset: 0, historyOffset: 0});
    // Everything a rig's page fetches for itself, rather than rendering from
    // the detail: until each answers, the last rig's rows would stand under
    // this rig's name -- and the runs card carries controls that act on
    // whichever rig is open, so they would have been that rig's rows and this
    // rig's buttons.
    ["rig-runs", "rig-history"].forEach(id => {
      forgetRendered($(id));
      renderSection($(id), '<p class="muted">Loading…</p>');
    });
    $("rig-logs").hidden = true;
  }
  if (shell().localRigPage && name === "local") {
    // This rig, in the same shape a portal serves for one of its rigs (the
    // rig view): one page draws either, and a rig that connects to a farm
    // shows there what it shows here.
    try {
      const view = await api("/api/v1/view");
      if (view.github) lastGithubSummary = view.github;
      rigPage.detail = {...view, ...localRig(), health: view.health, config: view.config,
        // The rig's name is the view's: its host, or what Settings gave it.
        label: view.name && view.name !== "local" ? view.name : shell().localRigLabel,
        setup: view.setup || [], commands: view.commands || [], inventory: view.inventory || {}};
    } catch {
      // A key that may not read the host's configuration (an account on a
      // node) still gets the rig's page from what the status carries.
      rigPage.detail = {...localRig(), health: lastStatus?.health || null, config: null,
        setup: [], commands: [], inventory: {
        missing: lastInventory.missing || [], unregistered: lastInventory.unregistered || [], probe_errors: lastInventory.probe_errors || [],
        instruments: lastInventory.instruments || [], missing_instruments: lastInventory.missing_instruments || []}};
    }
  } else {
    try { rigPage.detail = await api(`/api/v1/rigs/${encodeURIComponent(name)}`); }
    catch (error) {
      if (rigPage.name !== name) return;
      rigPage.detail = null;
      $("rig-title").textContent = name;
      renderSection($("rig-subtitle"), "");
      renderSection($("rig-capabilities"), "");
      renderSection($("rig-actions"), "");
      renderSection($("rig-summary"), `<p class="failure-summary">${escapeHtml(error.message)}</p><p class="muted">It may have been deleted. <a href="#rigs">All rigs</a></p>`);
      // Every section of the page but the one carrying the failure. Listed by
      // hand, this went stale the moment a section was added: the list had
      // grown a history card and not a setup card, so a rig that could not be
      // opened showed the last rig's capabilities under the error.
      rigSections().forEach(node => { node.hidden = node.id !== "rig-summary"; });
      // The failure is rendered into the summary, which belongs to Overview:
      // held on another tab, an operator opening a deleted rig would be shown
      // an empty page rather than what happened to it, with no tab bar to get
      // back -- the bar is only rendered for a rig that answered.
      $("rig-tabs").hidden = true;
      rigPage.tab = "overview";
      applyRigTab();
      return;
    }
  }
  if (rigPage.name !== name) return;
  if (!rigPage.detail.pending) joinTokens.delete(name);
  // The tab is kept across rigs on purpose, but not every rig offers every
  // tab: Activity is the owner's, and a rig lent to this caller has no
  // Activity button. Kept as it was, the button vanished while the section
  // stayed shown and the fetch behind it still ran, so an operator walking
  // from their own rig to a shared one landed on an error panel with no tab
  // lit. A tab this rig does not offer falls back to the one every rig has.
  if (!rigTabsFor(rigPage.detail).some(tab => tab.id === (rigPage.tab || "overview"))) {
    rigPage.tab = "overview";
  }
  renderRig();
  if (!rigPage.detail.pending) {
    // The page the operator is on, not the first one: the poll comes through
    // here every few seconds, and reading page three of the runs was not
    // possible while every poll put them back to page one.
    loadRigRuns(name, rigPage.runsOffset || 0);
    shell().rigWebhooksLoad(name);
    if (rigPage.tab === "activity") loadRigHistory(name, arrived ? 0 : (rigPage.historyOffset || 0));
  }
}

function renderRig() {
  const rig = rigPage.detail;
  if (!rig) return;
  const pending = Boolean(rig.pending);
  $("rig-title").textContent = rigLabel(rig);
  renderSection($("rig-subtitle"), rigSubtitle(rig));
  // What it can do, under its name and on every tab: the boards, the access
  // point and its channel, the broker, who is told. It was a card two tabs
  // away, and "does this rig have a broker" is the first question asked of
  // a rig, not the fifth.
  renderSection($("rig-capabilities"), pending ? "" : capabilityChips(rig.setup, {clickable: true}));
  renderSection($("rig-actions"), pending ? shell().pendingActions(rig) : rigActions(rig));
  if (rigPage.editing !== "details") renderSection($("rig-summary"), pending ? shell().pendingSummary(rig) : rigSummary(rig));
  $("rig-summary")?.querySelector(".rig-visibility-select")?.addEventListener("change", event => changeVisibility(rig, event.target.value, event.target));
  // From the page rather than from a list here, for the same reason as the
  // failure path: a section added to index.html would otherwise have to be
  // remembered in two places. The summary carries a pending rig's join
  // command, and the live run and the logs decide for themselves below.
  rigSections().forEach(node => {
    if (node.id === "rig-summary") return;
    if (pending) node.hidden = true;
    else if (!["rig-live", "rig-logs"].includes(node.id)) node.hidden = false;
  });
  $("rig-tabs").hidden = pending;
  if (!pending) renderRigTabs(rig);
  if (pending) {
    $("rig-live").hidden = true;
    $("rig-logs").hidden = true;
    // A rig that has not joined has no tabs to choose from, and its summary
    // is the whole page: its join state and the command to run, which is
    // shown once. Left on the tab the last rig was on, the page filtered that
    // away and offered no tab bar to bring it back.
    rigPage.tab = "overview";
    applyRigTab();
    return;
  }
  renderRigLive(rig);
  renderSection($("rig-setup"), rigSetup(rig));
  if (renderSection($("rig-boards"), rigBoards(rig))) linkRows($("rig-boards"));
  renderSection($("rig-run-controls"), runControls(rig));
  renderSection($("rig-health"), rigHealth(rig));
  renderSection($("rig-activity"), rigActivity(rig));
  if (rigPage.editing !== "settings") renderSection($("rig-settings"), rigSettings(rig));
  // A credential being typed is never re-rendered away by the poll.
  renderSection($("rig-network"), rigNetwork(rig));
  // A credential being typed is never re-rendered away by the poll -- every
  // editor of a channel now lives in this one card.
  if (!["channel-add", "channel-events", "provider", "provider-policy"].includes(rigPage.editing)) {
    renderSection($("rig-channels"), rigChannels(rig));
  }
  renderRigLogs();
  applyRigTab();
}

function rigSubtitle(rig) {
  if (rig.pending) {
    const state = joinState(rig);
    return [`<span class="state ${state.tone}">${state.label}</span>`, rig.location ? escapeHtml(rig.location) : ""].filter(Boolean).join(" · ");
  }
  const state = workerState(rig);
  const release = rigRelease(rig);
  return [`<span class="state ${state.tone}">${state.label}</span>`, `${rig.local ? "Version" : "Release"} <strong>${escapeHtml(rig.version || "unknown")}</strong>`,
    rig.local ? "" : `<span class="state ${release.tone}">${escapeHtml(release.label)}</span>`,
    // On the world page, and for anyone: worth saying beside its name.
    rig.visibility && rig.visibility !== "private" ? `<span class="state good">${escapeHtml(rig.visibility)}</span>` : "",
    rig.location ? escapeHtml(rig.location) : ""].filter(Boolean).join(" ");
}

// escapeHtml, SETUP_STATE and capabilityChips are chips.js: one renderer
// for a rig's chips here and on the world page, which has no sign-in.

// What the rig is and where, with the editor in place of it while open.
function detailsEditor(rig) {
  return `<form id="rig-details-form" class="settings-form rig-details-form">
    ${rig.pending ? `<label>Name<input name="name" required pattern="[a-z0-9][a-z0-9._-]{0,31}" maxlength="32" autocomplete="off" spellcheck="false" value="${escapeHtml(rig.name)}"></label>` : ""}
    <label>Description<input name="description" maxlength="200" autocomplete="off" placeholder="What it is: the host, its boards" value="${escapeHtml(rig.description || "")}"></label>
    <label>Location<input name="location" maxlength="100" autocomplete="off" placeholder="Where it is" value="${escapeHtml(rig.location || "")}"></label>
    <div class="row-actions"><button type="submit">Save</button><button type="button" class="secondary rig-edit-cancel">Cancel</button>${rig.pending ? "" : '<small class="muted">A rig that has joined keeps its name: its key, its boards and its runs are named for it.</small>'}</div>
  </form>`;
}

function rigSummary(rig) {
  const description = rig.description ? `<p class="rig-description">${escapeHtml(rig.description)}</p>` : "";
  const editing = rigPage.editing === "details";
  return `${editing ? detailsEditor(rig) : description}${workerFacts(rig, {long: true})}${rig.drained ? `<p class="queue-paused">Drained ${rig.drained.at ? escapeHtml(shortWhen(rig.drained.at)) : ""} by ${escapeHtml(rig.drained.by || "an operator")}${rig.drained.reason ? `: ${escapeHtml(rig.drained.reason)}` : ""}. It takes no new run; what it runs carries on.</p>` : ""}`;
}

function rigActions(rig) {
  if (rig.local) {
    return `${canaryAvailable() ? '<button class="secondary rig-canary">Check every board</button>' : ""}<button class="secondary rig-rediscover"${rigBusy ? " disabled" : ""}>Rediscover</button>`;
  }
  const busy = kind => rigPage.busy.has(kind) ? " disabled" : "";
  const release = releaseState(rig);
  const running = Number(rig.running) > 0;
  const buttons = [];
  if (canaryAvailable() && rig.online) buttons.push(`<button class="secondary rig-canary">Check every board</button>`);
  buttons.push(`<button class="secondary admin-only rig-command" data-kind="rediscover"${busy("rediscover")}>Rediscover</button>`);
  // A rig is its owner's: the portal offers the install, it does not force it.
  if (release.tone !== "good" || rig.update?.state === "failed") buttons.push(`<button class="secondary admin-only rig-command" data-kind="update_install"${busy("update_install")} title="Install the release this farm names on the rig, now">Install update</button>`);
  if (release.tone !== "good") buttons.push(`<button class="secondary admin-only rig-command" data-kind="update_now"${busy("update_now")} title="Ask the rig to look at the farm's release again">Check now</button>`);
  buttons.push(rig.drained
    ? `<button class="secondary admin-only rig-resume">Resume</button>`
    : `<button class="secondary admin-only rig-drain">Drain</button>`);
  buttons.push(`<button class="secondary admin-only rig-command" data-kind="logs"${busy("logs")}>Logs</button>`);
  buttons.push(`<button class="secondary admin-only rig-command" data-kind="restart"${busy("restart") || (running ? " disabled" : "")} title="${running ? "It is running a job: drain it and let the run end first" : "Restart the rig's farm service"}">Restart</button>`);
  // What a rig says about itself and whether it exists are its owner's
  // (FarmManager.may_manage_rig; the service refuses the rest with "no such
  // rig"). The host controls above stay an operator's.
  if (ownsRig(rig)) {
    buttons.push(`<button class="secondary rig-edit">Edit</button>`);
    const deletable = rig.drained || !rig.online;
    buttons.push(`<button class="danger rig-delete"${deletable ? "" : ' disabled title="Drain it first: a rig taking work is not deleted"'}>Delete</button>`);
  }
  return buttons.join("");
}

function rigBoards(rig) {
  const boards = boardsOf(rig);
  const inventory = rig.inventory || {};
  const rows = boards.map(board => {
    const state = boardState(board);
    return `<tr class="clickable" data-href="${boardHref(board.id)}"><td><a class="row-link" href="${boardHref(board.id)}">${escapeHtml(board.id)}</a><small>${escapeHtml(board.mac || "")}</small></td><td>${escapeHtml(familyLabel(board.target))}<small>${escapeHtml(board.details?.description || board.chip || "")}</small></td><td><span class="state ${state.tone}">${state.label}</span>${board.held_by ? `<small>run ${escapeHtml((board.held_by.job_id || "").slice(0, 8))}</small>` : ""}</td><td>${healthBadge(board)}</td><td><small>${escapeHtml(board.port || "")}</small><small>${escapeHtml(board.usb_path || "")}</small></td></tr>`;
  }).join("");
  const unregistered = (inventory.unregistered || []).map(device => `<li><div><strong>Unregistered ${escapeHtml(device.target)}</strong> <code>${escapeHtml(device.mac)}</code><small>${escapeHtml(device.port || "")}</small></div><form class="register-board admin-only" data-mac="${escapeHtml(device.mac)}"><input name="id" required pattern="[a-z0-9][a-z0-9._-]{0,31}" value="${escapeHtml(suggestedId(device))}" aria-label="Farm id"><button type="submit" class="secondary">Register</button></form></li>`).join("");
  const missing = (inventory.missing || []).map(id => `<li><div><strong>${escapeHtml(id)}</strong> <span class="state warn">MISSING</span><small>Registered, and not plugged in now.</small></div><button class="secondary admin-only unregister-board" data-id="${escapeHtml(id)}">Unregister</button></li>`).join("");
  const errors = (inventory.probe_errors || []).map(error => `<li><div><strong>${escapeHtml(error.port)}</strong> <span class="state bad">UNREADABLE</span><small>${escapeHtml(error.error)}</small></div></li>`).join("");
  const instruments = (inventory.instruments || []).map(item => `<li><div><strong>${escapeHtml(item.id)}</strong> <span class="state good">INSTRUMENT</span><small>${escapeHtml(item.kind)} · ${escapeHtml(item.port || "")}</small></div></li>`).join("");
  const extra = unregistered + missing + errors + instruments;
  return `<div class="title-row"><div><p class="eyebrow">BOARDS</p><h2>${boards.length} board${boards.length === 1 ? "" : "s"}</h2></div><span class="muted">${boardCounts(boards).available} available</span></div>${
    rows ? `<div class="table-wrap"><table class="fleet"><thead><tr><th>Board</th><th>Family · chip</th><th>State</th><th>Health check</th><th>Port</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p class="muted">No board reported.</p>'}${
    extra ? `<h3>Also on this rig</h3><ul class="queue-list rig-devices">${extra}</ul>` : ""}`;
}

// A check's name as an operator says it.
const CHECK_LABELS = {
  mode: "Hardware mode", python: "Python", tools: "Tools", runner_service: "Runner service",
  gateway_service: "Rig access point", gateway_credentials: "Access point password", gateway_endpoint: "Uplink probe", gateway_channel: "AP channel",
  farm_service: "Farm service", farm_endpoint: "Farm API", reverse_proxy: "Reverse proxy", disk: "Disk",
  host: "Load, memory and temperature", rig_lock: "Rig lock",
  disk_config: "Disk thresholds", pi_power: "Power and temperature", usb_power: "USB power switching",
  board_map: "Boards", board_config: "Board settings", backup: "Backup",
};
function checkLabel(name) {
  return CHECK_LABELS[name] || String(name || "").replace(/_/g, " ").replace(/^./, first => first.toUpperCase());
}

// The rig's host health, compact: the verdict and what is not ok, each with
// its reason; every check behind one click.
// A rig is a computer in a cupboard: what it is doing is four numbers, and
// the farm knew none of them. Read from the host check's own data, so a rig
// that reports no metrics shows nothing rather than zeros.
function hostMetrics(rig) {
  const host = (rig.health?.checks || []).find(check => check.name === "host");
  if (!host || host.cpus === undefined && host.memory_total === undefined) return null;
  const hours = Math.round((host.uptime_seconds || 0) / 3600);
  const up = hours >= 48 ? `${Math.round(hours / 24)}d` : `${hours}h`;
  const tone = value => value === "ok" ? "" : value === "degraded" ? "warn" : "bad";
  const cells = [
    host.load_1 !== undefined
      ? [`${host.load_1.toFixed(2)}`, `load · ${host.cpus} CPU${host.cpus === 1 ? "" : "s"}`] : null,
    host.memory_used_percent !== undefined
      ? [`${host.memory_used_percent}%`,
         `memory of ${(host.memory_total / 1e9).toFixed(1)} GB`] : null,
    host.temperature_c !== undefined ? [`${host.temperature_c.toFixed(1)}°`, "SoC temperature"] : null,
    host.uptime_seconds !== undefined ? [up, "uptime"] : null,
  ].filter(Boolean);
  return `<div class="host-metrics ${tone(host.status)}">${cells.map(([value, label]) =>
    `<div><strong>${escapeHtml(value)}</strong><small>${escapeHtml(label)}</small></div>`).join("")}</div>`;
}

function rigHealth(rig) {
  const report = rig.health && typeof rig.health === "object" ? rig.health : null;
  const checks = report?.checks || [];
  const status = workerHealth(rig);
  const when = report?.timestamp ? `checked ${whenSpan(report.timestamp)}` : "";
  const heading = `<div class="title-row"><div><p class="eyebrow">HOST</p><h2>Health</h2></div><span><span class="muted">${when}</span> <span class="state ${statusClass(status)}">${escapeHtml(status)}</span></span></div>`;
  if (!checks.length) return `${heading}<p class="muted">The rig has not reported its host health checks.</p>`;
  const metrics = hostMetrics(rig) || "";
  const counts = {};
  checks.forEach(check => { counts[check.status] = (counts[check.status] || 0) + 1; });
  const tone = value => value === "ok" ? "good" : value === "degraded" ? "warn" : "bad";
  const summary = Object.entries(counts).sort((a, b) => ["unhealthy", "degraded", "ok"].indexOf(a[0]) - ["unhealthy", "degraded", "ok"].indexOf(b[0]))
    .map(([value, count]) => `<span><span class="dot ${tone(value)}"></span>${escapeHtml(count)} ${escapeHtml(value)}</span>`).join("");
  const problems = checks.filter(check => check.status !== "ok");
  return `${heading}${metrics}<div class="health-summary">${summary}</div>${
    problems.length
      ? `<ul class="health-problems">${problems.map(check => `<li><span class="dot ${tone(check.status)}"></span><strong>${escapeHtml(checkLabel(check.name))}</strong><small title="${escapeHtml(check.message || "")}">${escapeHtml(shortDetail(check.message, 140))}</small></li>`).join("")}</ul>`
      : `<p class="muted">Every check passes.</p>`}
    <details class="compact-more" data-keep="all-checks"><summary>All ${checks.length} checks</summary><div class="check-chips">${checks.map(check => `<span class="check-chip" title="${escapeHtml(`${check.status}: ${check.message || ""}`)}"><span class="dot ${tone(check.status)}"></span>${escapeHtml(checkLabel(check.name))}</span>`).join("")}</div></details>`;
}

const COMMAND_LABELS = {notify_set: "Set notification channel", notify_remove: "Turn notifications off", notify_test: "Send notification test", rediscover: "Rediscover", read_details: "Read chip", register: "Register board", unregister: "Unregister board", update_now: "Update now", restart: "Restart", logs: "Logs", configure: "Change settings", provider_set: "Set CallMeBot link", provider_remove: "Remove CallMeBot link", provider_test: "Send CallMeBot test"};

// The rig page answers four questions, and used to answer them all at once,
// in one scroll: what this rig is, what its boards are, what it has run, and
// what has been asked of it. One tab each, so a page opened to check a board
// is not first a page about releases.
// Which tab a section belongs to is written on the section itself
// (`data-tab` in index.html) and applied in CSS, so a renderer that runs after
// the tab was chosen cannot bring its section back. This list is the bar.
const RIG_TABS = [
  {id: "overview", label: "Overview"},
  {id: "boards", label: "Boards"},
  {id: "runs", label: "Runs"},
  // What has been done to this rig and by whom, which is the owner's. A rig
  // lent to this caller answers 403 for it, so the tab is not offered.
  {id: "activity", label: "Activity", owner: true},
  // Setup is the host as its owner configured it, and `rig_detail` sends a
  // borrower none of it -- so the tab drew an empty page and its settings
  // card offered writes that answer 404.
  {id: "setup", label: "Setup", owner: true},
];

// The tabs this rig offers this caller. `lent` comes from the rig itself
// (FarmManager.rig_detail), so a borrower and an owner are told apart by the
// same fact the service used to decide what to send.
function rigTabsFor(rig) {
  return RIG_TABS.filter(tab => !(tab.owner && rig && rig.lent));
}

function renderRigTabs(rig) {
  const current = rigPage.tab || "overview";
  const badge = {
    boards: typeof rig.boards === "number" ? rig.boards : null,
    setup: (rig.setup || []).filter(row => row.state === "broken").length || null,
  };
  $("rig-tabs").innerHTML = rigTabsFor(rig).map(tab =>
    `<button class="tab${tab.id === current ? " active" : ""}" data-tab="${escapeHtml(tab.id)}">`
    + `${escapeHtml(tab.label)}`
    + (badge[tab.id] != null ? ` <small${tab.id === "setup" ? ' class="bad"' : ""}>${escapeHtml(String(badge[tab.id]))}</small>` : "")
    + `</button>`).join("");
}

// Every section of a rig's page, from the page itself: a section is added to
// index.html with the tabs it belongs to, and nothing here has to be told.
function rigSections() {
  return [...document.querySelectorAll('.page[data-page="rig"] > [data-tab]')];
}

// One attribute, and the stylesheet does the rest. It was a sweep over the
// sections after every render, which lost every race with a renderer that
// finished later: the runs arriving, a command's result coming back.
function applyRigTab() {
  const page = document.querySelector('.page[data-page="rig"]');
  if (page) page.dataset.rigTab = rigPage.tab || "overview";
}

// A tab whose content is a page of its own is fetched when it is opened, not
// on every poll of a page nobody has that tab open on.
function goToRigTab(id) {
  if (!rigTabsFor(rigPage.detail).some(tab => tab.id === id)) return;
  rigPage.tab = id;
  renderRig();
  if (id === "activity" && rigPage.name) loadRigHistory(rigPage.name, 0);
}

function rigSetup(rig) {
  const rows = rig.setup || [];
  if (!rows.length) return `<div class="title-row"><div><p class="eyebrow">SETUP</p><h2>What this rig can do</h2></div></div>`
    + `<p class="muted">This rig has not reported its configuration yet.</p>`;
  const broken = rows.filter(row => row.state === "broken");
  const on = rows.filter(row => row.state === "on");
  const note = broken.length
    ? `<span class="state bad">${broken.length} set up and not working</span>`
    : `<span class="muted">${on.length} of ${rows.length} in use</span>`;
  const open = rigPage.setupOpen;
  const heading = `<div class="title-row"><div><p class="eyebrow">SETUP</p><h2>What this rig can do</h2></div>`
    + `<div class="row-actions">${note}<button class="secondary setup-toggle">${open ? "Hide details" : "Details"}</button></div></div>`;
  // Compact by default: the count and what is wrong, because the chips are
  // in the page heading now, on every tab. The full table -- what each
  // enables, where it is changed, the command -- is behind the toggle,
  // because a page that says everything at once says nothing at a glance.
  // What is wrong is said in the compact view too: it is the reason to look.
  const attention = broken.map(row =>
    `<li><strong>${escapeHtml(row.title)}</strong> — ${escapeHtml(row.summary)}</li>`).join("");
  if (!open) {
    return heading + (attention ? `<ul class="setup-attention">${attention}</ul>`
      : '<p class="muted">What it can do is under its name; what is off, and how each is turned on, is behind Details.</p>');
  }
  // Each row carries its capability's key, so a chip in the heading can
  // bring the reader to it.
  return `${heading}<div class="table-wrap"><table class="setup-table"><tbody>${rows.map(row => {
        const state = SETUP_STATE[row.state] || SETUP_STATE.unknown;
        const where = row.where === "portal" ? "on this page"
          : row.where === "deploy" ? "by a deploy" : "on the rig";
        const command = row.command && row.state !== "on"
          ? `<small><code>${escapeHtml(row.command)}</code></small>` : "";
        return `<tr id="setup-row-${escapeHtml(row.key)}"><td><strong>${escapeHtml(row.title)}</strong><small>${escapeHtml(row.enables || "")}</small></td>`
          + `<td class="nowrap"><span class="state ${state.tone}">${escapeHtml(state.label)}</span>`
          + `<small class="muted">${escapeHtml(where)}</small></td>`
          + `<td>${escapeHtml(row.summary)}`
          + (row.details || []).map(detail => `<small>${escapeHtml(detail)}</small>`).join("")
          + command + `</td></tr>`;
      }).join("")}</tbody></table></div>`;
}

// One command as a row, wherever it is listed: the card on the overview shows
// the last few, the Activity tab reads the whole history back a page at a time.
function commandRow(command) {
  // A sealed link is never returned by the portal; its placeholder and the
  // key it was sealed for say enough.
  const args = Object.entries(command.args || {}).filter(([key]) => !["settings", "sealed"].includes(key))
    .map(([key, value]) => key === "fingerprint" ? `for key ${String(value).slice(0, 16)}…` : `${key} ${value}`).join(", ")
    || (command.args?.settings ? Object.entries(command.args.settings).map(([key, value]) => `${key} = ${value}`).join(", ") : "");
  const tone = command.status === "done" ? "good" : ["failed", "expired"].includes(command.status) ? "bad" : "warn";
  return `<tr><td class="nowrap">${whenSpan(command.created_at)}</td><td><strong>${escapeHtml(COMMAND_LABELS[command.kind] || command.kind)}</strong>${args ? `<small>${escapeHtml(args)}</small>` : ""}</td><td>${escapeHtml(command.requested_by || "")}</td><td><span class="state ${tone}">${escapeHtml(command.status)}</span>${command.detail ? `<small title="${escapeHtml(command.detail)}">${escapeHtml(shortDetail(command.detail, 90))}</small>` : ""}</td></tr>`;
}

function rigActivity(rig) {
  if (rig.local) return `<div class="title-row"><div><p class="eyebrow">ACTIVITY</p><h2>What was asked of it</h2></div></div><p class="muted">A rig managed by a portal lists what was asked of it here. This host is managed on itself.</p>`;
  const commands = rig.commands || [];
  const rows = commands.map(commandRow).join("");
  // The card is the last few; the whole history is its own tab, because a
  // card that grows without end is a card nobody reads to the bottom of.
  const heading = `<div class="title-row"><div><p class="eyebrow">ACTIVITY</p><h2>What was asked of it</h2></div>`
    + `${rows ? '<button class="secondary rig-tab-go" data-go="activity">See all</button>' : ""}</div>`;
  return `${heading}${
    rows ? `<div class="table-wrap"><table class="compact"><tbody>${rows}</tbody></table></div>` : '<p class="muted">Nothing has been asked of this rig from the portal yet.</p>'}`;
}

// The Activity tab: everything ever asked of this rig, newest first.
async function loadRigHistory(name, offset = 0) {
  if (shell().localRigPage || rigPage.detail?.local) {
    return renderSection($("rig-history"), `<div class="title-row"><div><p class="eyebrow">ACTIVITY</p><h2>What was asked of it</h2></div></div>`
      + `<p class="muted">This host is managed on itself, not from a portal: there is no list of commands to page through.</p>`);
  }
  const limit = 25;
  // Which request this is. Two can be in flight for the same rig at once --
  // the operator clicks Older while the poll asks for the page it knows about
  // -- and the answer that arrives last is not the one that was asked for
  // last: the poll's page would undo the operator's click.
  const asked = rigPage.historyRequest = (rigPage.historyRequest || 0) + 1;
  const current = () => rigPage.name === name && rigPage.historyRequest === asked;
  // The page being asked for, recorded before the asking. A poll can already
  // be awaiting the rig's detail when the operator clicks Older; when it
  // resumes it starts its own request from this, and read after the answer
  // instead of before it, that would still be the page they clicked away from
  // -- a newer request for an older page, undoing the click.
  rigPage.historyOffset = Math.max(0, offset);
  let page;
  try { page = await api(`/api/v1/workers/${encodeURIComponent(name)}/commands?limit=${limit}&offset=${Math.max(0, offset)}`); }
  catch (error) {
    // A request for the rig that was left, or one already superseded, has
    // nothing to say about what is on screen: the guard belongs on the way a
    // request fails as much as on the way it succeeds.
    if (!current()) return;
    return renderSection($("rig-history"), `<p class="failure-summary">${escapeHtml(error.message)}</p>`);
  }
  if (!current()) return;
  rigPage.historyOffset = page.offset || 0;
  const commands = page.commands || [];
  const total = page.total ?? commands.length;
  const first = commands.length ? rigPage.historyOffset + 1 : 0;
  const last = rigPage.historyOffset + commands.length;
  renderSection($("rig-history"),
    `<div class="title-row"><div><p class="eyebrow">ACTIVITY</p><h2>Everything asked of this rig</h2></div>`
    + `<span class="muted">${escapeHtml(String(total))} in all</span></div>`
    + (commands.length
      ? `<div class="table-wrap"><table class="compact"><thead><tr><th>When</th><th>Command</th><th>By</th><th>How it went</th></tr></thead>`
        + `<tbody>${commands.map(commandRow).join("")}</tbody></table></div>`
        + `<div class="pager"><span class="muted">${escapeHtml(String(first))}–${escapeHtml(String(last))} of ${escapeHtml(String(total))}</span>`
        + `<button class="secondary history-page" data-offset="${Math.max(0, rigPage.historyOffset - limit)}"${rigPage.historyOffset ? "" : " disabled"}>Newer</button>`
        + `<button class="secondary history-page" data-offset="${rigPage.historyOffset + limit}"${last < total ? "" : " disabled"}>Older</button></div>`
      : '<p class="muted">Nothing has been asked of this rig from the portal yet.</p>'));
}

// The settings a portal may change on a rig (hil_config.REMOTE_SETTINGS), in
// the groups an operator thinks of them in, each with what it does. The rest
// of a rig's configuration -- its network, broker, backups, paths -- is set on
// the rig itself and shown below them, read-only.
const SETTING_GROUPS = [
  {title: "Runs", note: "How much work the rig takes at once.", fields: [
    {key: "queue.concurrency", label: "Runs at once", read: config => config?.service?.concurrency, min: 1, max: 16, unit: "", help: "Runs that may share its boards at the same time."},
  ]},
  {title: "Retention", note: "What the rig deletes on its own, daily. Reports, JUnit and records always stay.", fields: [
    {key: "retention.enabled", label: "Delete old evidence", read: config => config?.retention?.enabled, help: "Off: captures and logs are kept for good."},
    {key: "retention.run_evidence_days", label: "Serial and broker captures", read: config => config?.retention?.run_evidence_days, min: 1, max: 3650, unit: "days"},
    {key: "retention.log_days", label: "Job logs", read: config => config?.retention?.log_days, min: 1, max: 3650, unit: "days"},
    {key: "retention.keep_newest_runs", label: "Newest runs always kept", read: config => config?.retention?.keep_newest_runs, min: 0, max: 100000, unit: "runs"},
    {key: "retention.workspace_days", label: "Stale checkouts", read: config => config?.retention?.workspace_days, min: 1, max: 365, unit: "days"},
  ]},
  {title: "Host health", note: "When the rig calls itself degraded or unhealthy.", fields: [
    {key: "health.interval_minutes", label: "Check every", read: config => config?.health?.interval_minutes, min: 1, max: 1440, unit: "min"},
    {key: "health.minimum_boards", label: "Minimum boards", read: config => config?.health?.minimum_boards, min: 1, max: 100, unit: "boards", help: "Fewer connected, and the rig reports itself degraded."},
    {key: "health.disk_warn_percent", label: "Disk warning at", read: config => config?.health?.disk_warn_percent, min: 1, max: 99, unit: "%"},
    {key: "health.disk_critical_percent", label: "Disk critical at", read: config => config?.health?.disk_critical_percent, min: 2, max: 100, unit: "%"},
  ]},
];
const REMOTE_FIELDS = SETTING_GROUPS.flatMap(group => group.fields);

function settingText(field, value) {
  if (value === undefined || value === null) return '<span class="muted">not set</span>';
  if (field.min === undefined) return value ? "on" : "off";
  return `${escapeHtml(value)}${field.unit ? ` ${escapeHtml(field.unit)}` : ""}`;
}

function rigSettings(rig) {
  const config = rig.config;
  const editing = rigPage.editing === "settings";
  const editable = !rig.local && isAdmin();
  const reported = rig.config_at ? `reported ${relativeWhen(rig.config_at)}` : rig.local ? "read from this host" : "";
  const heading = `<div class="title-row"><div><p class="eyebrow">CONFIGURATION</p><h2>Settings</h2></div><div class="row-actions"><span class="muted">${escapeHtml(reported)}</span>${editable && config && !editing ? '<button class="secondary settings-edit">Edit settings</button>' : ""}</div></div>`;
  if (!config) return `${heading}<p class="muted">This rig has not reported its configuration.</p>`;
  const running = Number(rig.running) > 0;
  const groups = SETTING_GROUPS.map(group => `<section class="setting-group"><h3>${escapeHtml(group.title)}</h3><p class="muted">${escapeHtml(group.note)}</p>${group.fields.map(field => {
    const value = field.read(config);
    const about = `<div><strong>${escapeHtml(field.label)}</strong>${field.help ? `<small class="muted">${escapeHtml(field.help)}</small>` : ""}</div>`;
    if (!editing) return `<div class="setting">${about}<span class="value">${settingText(field, value)}</span></div>`;
    const input = field.min === undefined
      ? `<input type="checkbox" name="${field.key}"${value ? " checked" : ""} data-was="${value ? "1" : "0"}">`
      : `<input type="number" name="${field.key}" min="${field.min}" max="${field.max}" value="${value ?? ""}" data-was="${value ?? ""}" required>${field.unit ? `<em>${escapeHtml(field.unit)}</em>` : ""}`;
    return `<label class="setting">${about}<span class="field">${input}</span></label>`;
  }).join("")}</section>`).join("");
  const body = editing
    ? `<form id="rig-settings-form"><div class="setting-groups">${groups}</div>
        <div class="settings-footer"><span id="settings-changes" class="muted">No changes yet</span><button type="submit"${running ? ' disabled title="It is running a job: drain it and let the run end first"' : ""}>Apply on ${escapeHtml(rig.name)}</button><button type="button" class="secondary settings-cancel">Cancel</button></div>
        <p class="muted">${running ? "It is running a job, and applying restarts its service: drain it and let the run end first. " : ""}The rig checks them, keeps them in its own configuration file and restarts its service to take them.</p></form>`
    : `<div class="setting-groups">${groups}</div>${rig.local ? '<p class="muted">Set on this host: <code>sudo alteriom-hil-admin config set</code>, or <code>/etc/alteriom-hil/config.yaml</code>.</p>' : ""}`;
  // The network and the broker have a card of their own now; the rest of
  // what a rig decides on itself stays folded here.
  const hostSections = renderConfigSections(config, {skip: ["health", "retention", "gateway", "mqtt"]});
  return `${heading}${body}${hostSections ? `<details class="host-config" data-keep="host-config"><summary>Set on the rig itself <small class="muted">network, broker, backups, notifications, paths, service</small></summary>${hostSections}</details>` : ""}`;
}

// ---- A rig's CallMeBot link, set from its page and sealed end to end ----------------------
// The link is a credential (docs/providers.md). The page never sends it: it
// encrypts it to the rig's own public key (RSA-OAEP, SHA-256) in the browser,
// and only the ciphertext goes to the portal, which relays it to the rig. The
// portal relays the public key too, so the fingerprint shown here is what an
// owner compares with `sudo alteriom-hil-admin providers seal-key` on the rig.
// The two policy settings are ordinary remote settings (hil_config.REMOTE_SETTINGS),
// applied with `configure`.
const PROVIDER_FIELDS = [
  {key: "providers.callmebot.send", label: "Sends real messages", read: config => config?.callmebot?.send, choices: ["never", "release", "always"],
    help: "never: the row always skips. release: only release builds. always: every run that reaches it."},
  {key: "providers.callmebot.max_per_day", label: "Messages per day", read: config => config?.callmebot?.max_per_day, min: 1, max: 50, unit: "per UTC day"},
];
const CALLMEBOT_HOST = "api.callmebot.com";

// Why a pasted link is not one the rig would store, or null. The rig checks it
// again (alteriom_hil.providers.parse_callmebot_link); this only spares a
// round trip. Never repeats the link.
function callmebotLinkProblem(text) {
  const value = String(text || "").trim();
  if (!value) return "the link is empty";
  if (/\s/.test(value)) return "the link must be one line with no spaces";
  let url;
  try { url = new URL(value); } catch { return "the link is not a URL"; }
  if (url.protocol !== "https:") return "the link must use https";
  if (url.username || url.password) return "the link must not carry a user name or password";
  if (url.hostname !== CALLMEBOT_HOST || !["", "443"].includes(url.port)) return `the link must point at ${CALLMEBOT_HOST}`;
  if (url.pathname !== "/whatsapp.php") return "the link must be the WhatsApp API, /whatsapp.php";
  if (value.includes("#")) return "the link must not have a #fragment";
  const query = value.includes("?") ? value.slice(value.indexOf("?") + 1) : "";
  const names = [];
  const params = {};
  for (const part of query ? query.split("&") : []) {
    const at = part.indexOf("=");
    if (at <= 0) return "the link's query string is malformed";
    let name, raw;
    try { name = decodeURIComponent(part.slice(0, at)); raw = decodeURIComponent(part.slice(at + 1)); }
    catch { return "the link's query string is malformed"; }
    names.push(name);
    params[name] = raw;
  }
  if ("text" in params) return "the link must not include text=; the validation adds its own message";
  if (names.some(name => name !== "phone" && name !== "apikey")) return "the link has parameters the rig does not know";
  for (const name of ["phone", "apikey"]) {
    if (names.filter(item => item === name).length > 1) return `the link names ${name} more than once`;
  }
  if (!params.phone) return "the link has no phone";
  if (!params.apikey) return "the link has no apikey";
  if (!/^\+?[0-9]{6,20}$/.test(params.phone)) return "the phone must be an international number: + and 6 to 20 digits";
  if (!/^[A-Za-z0-9_-]{4,64}$/.test(params.apikey)) return "the apikey must be 4 to 64 letters, digits, - or _";
  return null;
}

// Seal text to a rig's public key: RSA-OAEP with SHA-256 (and MGF1-SHA-256,
// which WebCrypto always pairs with it) -- what the rig opens with
// `openssl pkeyutl -decrypt -pkeyopt rsa_padding_mode:oaep -pkeyopt rsa_oaep_md:sha256
// -pkeyopt rsa_mgf1_md:sha256`. The fingerprint is computed here from the very
// key the text is sealed to, not taken from what was reported beside it.
async function sealProviderLink(spkiBase64, text) {
  const der = Uint8Array.from(atob(spkiBase64), character => character.charCodeAt(0));
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", der));
  const fingerprint = Array.from(digest, byte => byte.toString(16).padStart(2, "0")).join("");
  const key = await crypto.subtle.importKey("spki", der, {name: "RSA-OAEP", hash: "SHA-256"}, false, ["encrypt"]);
  const sealed = new Uint8Array(await crypto.subtle.encrypt({name: "RSA-OAEP"}, key, new TextEncoder().encode(text)));
  let binary = "";
  sealed.forEach(byte => { binary += String.fromCharCode(byte); });
  return {sealed: btoa(binary), fingerprint};
}

const groupedFingerprint = value => String(value || "").match(/.{1,4}/g)?.join(" ") || "";

// What this rig can send a message through. A channel is declared, not coded
// around (alteriom_hil.notify): this offers what the rig supports and asks for
// what that channel needs, so the next one is a row here rather than a page.
const CHANNEL_KINDS = {
  telegram: {
    title: "Telegram",
    secret: "Bot token",
    secretHelp: "From @BotFather. Kept in a root-owned file on the rig; the portal only relays it sealed.",
    fields: [{name: "chat_id", label: "Chat", help: "The chat to send to: 12345678, or -1001234567890 for a group.",
              pattern: "-?[0-9]{1,20}"}],
    how: "Talk to @BotFather for a bot and its token, message the bot, then read the chat id from api.telegram.org/bot<token>/getUpdates.",
  },
  callmebot: {
    title: "CallMeBot (WhatsApp)",
    secret: "CallMeBot link",
    secretHelp: "The link you already use for runs. On a rig that has one stored, leave this empty and it is reused.",
    optionalSecret: true,
    fields: [],
    how: "Message the CallMeBot bot on WhatsApp for an API key; the same link a board sends through during a run.",
  },
  webhook: {
    title: "Webhook",
    secret: "Webhook URL",
    secretHelp: "https only. Slack and Discord put the token in the URL, which is why it is sealed and kept in a file.",
    fields: [{name: "format", label: "Shape", help: "How the body is written.",
              options: ["slack", "discord", "json"]}],
    how: "A Slack incoming webhook, a Discord channel webhook, or any endpoint that takes JSON — your own ingestion connector included.",
  },
};

// The rig's own network: the access point its boards associate with, the
// probe they fetch, the broker they publish to. It was folded into "set on the
// rig itself" with everything else, which is where an operator went looking
// after every skipped uplink row.
function rigNetwork(rig) {
  const config = rig.config;
  const gateway = config?.gateway || {};
  const mqtt = config?.mqtt || {};
  const checks = Object.fromEntries((rig.health?.checks || []).map(check => [check.name, check]));
  const tone = value => value === "ok" ? "good" : value === "degraded" ? "warn" : "bad";
  const verdict = name => {
    const check = checks[name];
    return check ? `<span class="state ${tone(check.status)}">${escapeHtml(check.status)}</span>` : "";
  };
  const heading = `<div class="title-row"><div><p class="eyebrow">RIG NETWORK</p><h2>What the boards connect to</h2></div>`
    + `<span class="muted">${escapeHtml(gateway.enabled ? "access point on" : "no access point")}</span></div>`;
  if (!config) return `${heading}<p class="muted">This rig has not reported its configuration.</p>`;
  const rows = [
    ["Access point", gateway.enabled === undefined ? '<span class="muted">not reported</span>'
      : gateway.enabled ? `${escapeHtml(gateway.ssid || "")} ${verdict("gateway_service")}` : '<span class="muted">off</span>',
      gateway.enabled ? "The boards' radio checks scan for and join this." : "The radio, uplink and queue rows skip without it."],
    gateway.enabled ? ["Channel", `${escapeHtml(String(gateway.channel ?? "?"))} ${verdict("gateway_channel")}`,
      "Must equal the mesh's channel, or a bridge splits the mesh."] : null,
    gateway.enabled ? ["Uplink probe", `<code>${escapeHtml(gateway.endpoint || "")}</code> ${verdict("gateway_endpoint")}`,
      "What a board fetches to prove it reached the Internet through the rig."] : null,
    gateway.enabled ? ["Password", `<code>${escapeHtml(gateway.password_file || "")}</code> ${verdict("gateway_credentials")}`,
      "The file, never the password."] : null,
    ["Broker", mqtt.enabled === undefined ? '<span class="muted">not reported</span>'
      : mqtt.enabled ? `<code>${escapeHtml(mqtt.url || "")}</code>` : '<span class="muted">off</span>',
      mqtt.enabled ? "A board publishes here in the queue check." : "The queue row skips without it."],
  ].filter(Boolean);
  return `${heading}${viewGroup("", rig.local
    ? "Set on this host: sudo ./runner/setup-gateway-network.sh, then alteriom-hil-admin config set gateway.enabled true."
    : "Set on the rig itself; the commands are on its setup card.", rows)}`;
}

// What a rig may be told to say something about (alteriom_hil.notify.EVENTS).
// Which of them it actually sends is its owner's to choose, from here.
const NOTIFY_EVENTS = {
  queue_paused: {title: "The queue stopped", help: "It stopped taking runs -- after repeated failures, or because someone paused it."},
  board_red: {title: "A board went red", help: "A board failed its health check."},
  host_unhealthy: {title: "The host is unhealthy", help: "Its disk, its temperature, or boards that are no longer there."},
};

function notifyEvents(notify) {
  // The rig reports them joined, and "unset" means all of them.
  const said = typeof notify.events === "string" ? notify.events.split(",") : (notify.events || []);
  const chosen = said.map(item => String(item).trim()).filter(item => item in NOTIFY_EVENTS);
  return chosen.length ? chosen : Object.keys(NOTIFY_EVENTS);
}

// Only what is actually set up is listed. The card for each says what it is,
// where its credential lives, how the last message went, and carries its own
// buttons -- rather than one set of buttons at the top that could only ever
// mean the one channel.
// Everything this rig can send through, in one list. There used to be two:
// a "channel" the rig reported problems on, and a "provider" a board sent
// through during a run -- with the provider's card, further down the page,
// serving as the details and the editor of a channel listed above it. They are
// one thing seen from two sides: a credential the rig holds, and what it is
// used for. A card says both, and opens to its own details and buttons.
function rigChannels(rig) {
  const config = rig.config;
  // Every channel the rig reports. It held one and reported a mapping; it can
  // hold several now and reports a list, with the first still under the old
  // name for anything written against that.
  const channels = config?.notify_channels || (config?.notify?.channel ? [{...config.notify, id: "1"}] : []);
  const notify = channels[0] || {};
  const callmebot = config?.callmebot || {};
  const sealKey = config?.seal_key;
  const manageable = !rig.local && isAdmin();
  const running = Number(rig.running) > 0;
  const busy = ["notify_set", "notify_remove", "notify_test", "configure"].some(kind => rigPage.busy.has(kind));
  const providerBusy = ["provider_set", "provider_remove", "provider_test"].some(kind => rigPage.busy.has(kind));
  const channel = notify.channel || "webhook";
  // `link` rather than `url_file`: removing a link deletes the file and leaves
  // the path in the configuration, and a channel that says it is on while
  // sending nothing is worse than one that says it is broken.
  const callmebotLink = Boolean(callmebot.link);
  // Whether anything here can send at all, over however many channels.
  const usable = entry => (entry.channel || "webhook") === "callmebot"
    ? callmebotLink
    : Boolean((entry.channel || "webhook") === "telegram" ? entry.token_file : entry.webhook_url_file);
  const configured = channels.some(usable);
  const editing = rigPage.editing;
  const canAdd = Boolean(sealKey) || callmebotLink;
  const forms = ["channel-add", "channel-events", "provider", "provider-policy"];
  const heading = `<div class="title-row"><div><p class="eyebrow">CHANNELS</p><h2>What this rig can send through</h2></div><div class="row-actions">${
    manageable && config && !forms.includes(editing) && canAdd
      ? `<button class="secondary channel-add">Add a notification</button>`
        + (callmebotLink || !sealKey ? "" : `<button class="secondary provider-set-open">${callmebot.url_file ? "Replace CallMeBot link" : "Add CallMeBot link"}</button>`)
      : ""}</div></div>`;
  if (!config) {
    // Not the same thing as nothing being set up, and the difference is the
    // whole question: a rig that has told the portal nothing may be fully
    // configured on the rig itself.
    return `${heading}<p class="muted">${escapeHtml(rigLabel(rig))} has not reported its configuration to the portal yet, so what it is set up to send cannot be shown here. It reports at startup and every ten minutes.</p>`;
  }
  // Every editor opens here, beside the card it belongs to, rather than in a
  // section further down that a button appeared to do nothing to.
  if (editing === "channel-add") return `${heading}${channelForm(rig, sealKey, running)}`;
  if (editing === "channel-events") {
    // The channel whose button was clicked, not whichever is first.
    const chosen = channels.find(entry => entry.id === rigPage.channelEditing) || notify;
    return `${heading}${channelEventsForm(rig, chosen, running)}`;
  }
  if (editing === "provider") return `${heading}${providerLinkForm(rig, sealKey, running)}`;
  if (editing === "provider-policy") return `${heading}${providerPolicyForm(rig, running)}`;
  const pending = channelChangePending(rig);
  const waiting = pending
    ? `<p class="queue-paused">${escapeHtml(rigLabel(rig))} has applied the change and not yet reported its configuration — it does that within a minute. Until then this is what it was set to before.</p>`
    : "";

  const open = rigPage.channelOpen;
  const card = (key, title, state, lines, actions, rows) => {
    const shown = open === key;
    return `<div class="channel-card"><div><strong>${escapeHtml(title)}</strong>`
      + `<span class="state ${state.tone}">${escapeHtml(state.label)}</span></div>`
      + lines.filter(Boolean).map(line => `<small>${line}</small>`).join("")
      + (shown && rows.length ? `<div class="setting-groups">${viewGroup("", "", rows)}</div>` : "")
      + `<div class="row-actions">`
      + `<button class="secondary channel-details" data-channel="${escapeHtml(key)}">${shown ? "Hide details" : "Details"}</button>`
      + (manageable && !pending ? actions.join("") : "")
      + `</div></div>`;
  };

  const cards = [];
  // One card per channel this rig notifies through. A rig with two Telegram
  // bots has two cards, each with its own buttons: "which one is off" is not
  // a question a single card could answer.
  channels.forEach(entry => {
    const kind = entry.channel || "webhook";
    const spec = CHANNEL_KINDS[kind] || CHANNEL_KINDS.webhook;
    if (!usable(entry)) return;
    const last = entry.last_delivery || null;
    const state = entry.enabled
      ? (last && last.ok === false ? {tone: "bad", label: "not working"} : {tone: "good", label: "on"})
      : {tone: "muted", label: "off"};
    const where = kind === "telegram" ? `chat ${escapeHtml(entry.chat_id || "?")}`
      : kind === "callmebot" ? "the rig's own CallMeBot link"
      : `${escapeHtml(entry.format || "slack")} webhook`;
    const chosen = notifyEvents(entry);
    const actions = [
      `<button class="secondary channel-test" data-id="${escapeHtml(entry.id)}"${busy || running ? " disabled" : ""}>Send test</button>`,
      `<button class="secondary channel-events" data-id="${escapeHtml(entry.id)}"${busy ? " disabled" : ""}>Events</button>`,
      `<button class="secondary channel-${entry.enabled ? "off" : "on"}" data-id="${escapeHtml(entry.id)}"${busy || running ? " disabled" : ""}>Turn ${entry.enabled ? "off" : "on"}</button>`,
      ...(kind === "callmebot" ? providerActions(rig, {providerBusy, running, sealKey}) : []),
    ];
    cards.push(card(`notify-${entry.id}`, spec.title, state, [
      `${where} · the rig tells you when something breaks`,
      `sends: ${escapeHtml(chosen.map(key => NOTIFY_EVENTS[key].title.toLowerCase()).join(", "))}`,
      last ? `last message ${escapeHtml(shortWhen(last.at) || "")}: ${last.ok ? "delivered" : escapeHtml(shortDetail(last.error || "failed", 120))}` : "",
    ], actions, [
      ...notifyRows(rig, entry, kind, callmebot),
      ...(kind === "callmebot" ? callmebotRows(rig) : []),
    ]));
  });
  // The stored link, when it is not also what the rig notifies through: a
  // board sends through it during a run rather than the farm about one.
  const notifiesThroughLink = channels.some(entry => (entry.channel || "webhook") === "callmebot");
  if (callmebot.url_file && (!notifiesThroughLink || !callmebotLink)) {
    const state = callmebotLink
      ? {tone: callmebot.send === "never" ? "muted" : "good", label: callmebot.send || "?"}
      : {tone: "bad", label: "no usable link"};
    cards.push(card("callmebot", "CallMeBot", state, [
      "a board sends through it during a run, not the rig about one",
      callmebotLink
        ? `${escapeHtml(String(callmebot.used_today ?? 0))} of ${escapeHtml(String(callmebot.max_per_day ?? "?"))} today · <code>${escapeHtml(callmebot.url_file)}</code>`
        : `the link was removed or does not parse · <code>${escapeHtml(callmebot.url_file)}</code>`,
    ], providerActions(rig, {providerBusy, running, sealKey}), callmebotRows(rig)));
  }
  if (!cards.length) {
    return `${heading}${waiting}<p class="muted">Nothing is set up: nobody is told when this rig has a problem.`
      + `${canAdd ? ` Add a channel above${sealKey ? " — Telegram, a webhook, or the rig's own CallMeBot link." : ": it can send through the CallMeBot link it already holds."}`
        : " The rig has no seal key yet, so a credential cannot be sealed for it here: <code>sudo alteriom-hil-admin providers seal-key</code>."}</p>`;
  }
  return `${heading}${waiting}<div class="channel-cards">${cards.join("")}</div>`;
}

// What can be done to the rig's stored link, wherever it is listed.
function providerActions(rig, {providerBusy, running, sealKey}) {
  const callmebot = rig.config?.callmebot || {};
  if (rig.local || !isAdmin()) return [];
  const stop = providerBusy || running ? " disabled" : "";
  const waiting = running ? ' title="It is running a job: drain it and let the run end first"' : "";
  return [
    callmebot.link ? `<button class="secondary provider-test"${stop}${waiting}>${rigPage.busy.has("provider_test") ? "Sending…" : "Send a real message"}</button>` : "",
    callmebot.url_file ? `<button class="secondary provider-policy-open"${providerBusy ? " disabled" : ""}>Budget</button>` : "",
    sealKey ? `<button class="secondary provider-set-open"${providerBusy ? " disabled" : ""}>${callmebot.link ? "Replace link" : "Store a link"}</button>` : "",
    callmebot.link ? `<button class="secondary provider-remove"${stop}>Remove link</button>` : "",
  ].filter(Boolean);
}

// The rows behind a notifying channel's Details.
function notifyRows(rig, notify, channel, callmebot) {
  const credential = channel === "telegram" ? notify.token_file
    : channel === "callmebot" ? callmebot.url_file : notify.webhook_url_file;
  const sealKey = rig.config?.seal_key;
  return [
    ["Credential", credential ? `<code>${escapeHtml(credential)}</code>` : '<span class="muted">none stored</span>',
     "The file on the rig, never what is in it. Rotate it by setting the channel again."],
    channel === "telegram" ? ["Chat", plainValue(notify.chat_id), "Where the bot writes."] : null,
    channel === "webhook" ? ["Shape", plainValue(notify.format || "slack"), "How the body is written."] : null,
    ["Seal key", sealKey?.fingerprint
      ? `<code title="${escapeHtml(groupedFingerprint(sealKey.fingerprint))}">${escapeHtml(groupedFingerprint(sealKey.fingerprint.slice(0, 16)))}…</code>`
      : '<span class="muted">none reported</span>',
     sealKey ? "sha256 of the rig's public key: compare it on the rig before sealing a credential"
             : "Make one on the rig: sudo alteriom-hil-admin providers seal-key"],
  ].filter(Boolean);
}

// The rows behind the stored link's Details -- what the provider card was.
function callmebotRows(rig) {
  const config = rig.config;
  const callmebot = config?.callmebot || {};
  const configured = Boolean(callmebot.url_file);
  const checks = Object.fromEntries((rig.health?.checks || []).map(check => [check.name, check]));
  const tone = value => value === "ok" ? "good" : value === "degraded" ? "warn" : "bad";
  const lastTest = (rig.commands || []).find(command => command.kind === "provider_test");
  const route = checks.provider_callmebot_route;
  const linkCheck = checks.provider_callmebot;
  return [
    ["Link", !configured ? '<span class="muted">not configured</span>'
      : callmebot.link ? `<code>${escapeHtml(callmebot.link)}</code>` : '<span class="bad">no usable link stored</span>',
     configured ? "The key is never shown, the number only by its last two digits."
                : "The rig's CallMeBot row skips until a link is stored."],
    configured ? ["Sends real messages", plainValue(callmebot.send), "never, release (release builds only) or always"] : null,
    configured ? ["Used today", `${escapeHtml(callmebot.used_today ?? 0)} of ${escapeHtml(callmebot.max_per_day ?? "?")}`, "UTC day; a spent budget is a skip"] : null,
    linkCheck ? ["Link check", `<span class="state ${tone(linkCheck.status)}">${escapeHtml(linkCheck.status)}</span>`, shortDetail(linkCheck.message, 140)] : null,
    route ? ["Route to CallMeBot", `<span class="state ${tone(route.status)}">${escapeHtml(route.status)}</span>`, shortDetail(route.message, 140)] : null,
    lastTest ? ["Last real message", `<span class="state ${lastTest.status === "done" ? "good" : ["failed", "expired"].includes(lastTest.status) ? "bad" : "warn"}">${escapeHtml(lastTest.status)}</span> ${whenSpan(lastTest.finished_at || lastTest.created_at)}`,
      shortDetail(lastTest.detail || (["done", "failed", "expired"].includes(lastTest.status) ? "" : "waiting for the rig"), 160)] : null,
  ].filter(Boolean);
}

function providerLinkForm(rig, sealKey, running) {
  if (rig.local) {
    return '<p class="muted">Set on this host: <code>sudo alteriom-hil-admin providers set callmebot</code>.</p>'
      + `<div class="settings-footer"><button type="button" class="secondary provider-cancel">Close</button></div>`;
  }
  if (!sealKey) {
    return `<p class="muted">${escapeHtml(rig.name)} has not reported a seal key, so a link cannot be sealed for it here. On the rig: <code>sudo alteriom-hil-admin providers seal-key</code>, or set the link there.</p>`
      + `<div class="settings-footer"><button type="button" class="secondary provider-cancel">Close</button></div>`;
  }
  if (!window.isSecureContext || !globalThis.crypto?.subtle) {
    return `<p class="failure-summary">This page is not a secure context (https, or localhost), so the browser cannot seal a link here. Open the portal over https, or set it on the rig: <code>sudo alteriom-hil-admin providers set callmebot</code>.</p>`
      + `<div class="settings-footer"><button type="button" class="secondary provider-cancel">Close</button></div>`;
  }
  return `<form id="provider-link-form" autocomplete="off">
    <p>The link is sealed in this browser to ${escapeHtml(rig.name)}'s key; the portal only relays the ciphertext and cannot read it. Before you send, check that <code>sudo alteriom-hil-admin providers seal-key</code> on the rig prints this fingerprint:</p>
    <p><code class="fingerprint">${escapeHtml(groupedFingerprint(sealKey?.fingerprint))}</code></p>
    <label class="setting"><div><strong>CallMeBot link</strong><small class="muted">https://api.callmebot.com/whatsapp.php?phone=…&amp;apikey=… (no text=). Use a key dedicated to the rig.</small></div><span class="field"><input type="password" name="link" autocomplete="off" spellcheck="false" autocapitalize="off" required></span></label>
    <div class="settings-footer"><button type="submit"${running ? ' disabled title="It is running a job: drain it and let the run end first"' : ""}>Seal and send</button><button type="button" class="secondary provider-cancel">Cancel</button></div>
    <p class="muted">${running ? "It is running a job, and storing the link restarts its service: drain it and let the run end first. " : ""}The rig decrypts it straight into <code>alteriom-hil-admin providers set</code> and restarts its service.</p></form>`;
}

function providerPolicyForm(rig, running) {
  const config = rig.config;
  return `<form id="provider-policy-form"><div class="setting-groups"><section class="setting-group">${PROVIDER_FIELDS.map(field => {
    const value = field.read(config);
    const about = `<div><strong>${escapeHtml(field.label)}</strong>${field.help ? `<small class="muted">${escapeHtml(field.help)}</small>` : ""}</div>`;
    const input = field.choices
      ? `<select name="${field.key}" data-was="${escapeHtml(value ?? "")}">${field.choices.map(choice => `<option value="${choice}"${choice === value ? " selected" : ""}>${choice}</option>`).join("")}</select>`
      : `<input type="number" name="${field.key}" min="${field.min}" max="${field.max}" value="${escapeHtml(value ?? "")}" data-was="${escapeHtml(value ?? "")}" required><em>${escapeHtml(field.unit)}</em>`;
    return `<label class="setting">${about}<span class="field">${input}</span></label>`;
  }).join("")}</section></div>
    <div class="settings-footer"><button type="submit"${running ? ' disabled title="It is running a job: drain it and let the run end first"' : ""}>Apply on ${escapeHtml(rig.name)}</button><button type="button" class="secondary provider-cancel">Cancel</button></div></form>`;
}

// Which of the three things that go wrong this rig says anything about. A
// `configure` command, like any other remote setting: nothing is sealed, so
// changing the list never asks for the credential again.
function channelEventsForm(rig, notify, running) {
  const chosen = new Set(notifyEvents(notify));
  return `<form id="channel-events-form">
    <p class="muted">What ${escapeHtml(rigLabel(rig))} sends down its channel. Turning one off silences it on this rig only — the farm's own notifications are in Settings.</p>
    <div class="setting-groups"><section class="setting-group">${Object.entries(NOTIFY_EVENTS).map(([key, event]) =>
      `<label class="setting"><div><strong>${escapeHtml(event.title)}</strong><small class="muted">${escapeHtml(event.help)}</small></div>`
      + `<span class="field"><input type="checkbox" name="${escapeHtml(key)}"${chosen.has(key) ? " checked" : ""}></span></label>`).join("")}</section></div>
    <div class="settings-footer"><button type="submit"${running ? ' disabled title="It is running a job: drain it and let the run end first"' : ""}>Apply on ${escapeHtml(rig.name)}</button><button type="button" class="secondary channel-cancel">Cancel</button></div>
    <p class="muted">At least one: a channel that says nothing is turned off instead.</p></form>`;
}

function channelForm(rig, sealKey, running) {
  // Whether a credential can be sealed here at all: the browser needs a
  // secure context for WebCrypto, and the rig needs a key to seal to.
  const sealable = Boolean(window.isSecureContext && globalThis.crypto?.subtle
                           && sealKey?.spki && sealKey?.fingerprint);
  // What this rig can actually be given. CallMeBot is offered only by a rig
  // that has a usable link: it is the one channel with nothing to seal,
  // because it reuses what is stored. `link` rather than `url_file` --
  // removing a link leaves the path behind, and offering the channel over it
  // would turn notifications on against a credential file that is not there.
  // Having nothing to seal is also why it is the one channel a plaintext
  // dashboard, or a rig with no key, can still be pointed at.
  const offered = Object.entries(CHANNEL_KINDS).filter(([value, item]) =>
    (value !== "callmebot" || rig.config?.callmebot?.link) && (item.optionalSecret || sealable));
  if (!offered.length) {
    return `<p class="failure-summary">${escapeHtml(sealKey
      ? "This page is not a secure context (https, or localhost), so the browser cannot seal a credential here."
      : `${rigLabel(rig)} has not reported a seal key, so a credential cannot be sealed for it here.`)}`
      + ` Open the portal over https and give the rig a key (<code>sudo alteriom-hil-admin providers seal-key</code>), or set the channel on the rig: <code>sudo alteriom-hil-admin notify set --channel telegram --chat-id &lt;chat&gt;</code>.</p>`
      + `<div class="settings-footer"><button type="button" class="secondary channel-cancel">Close</button></div>`;
  }
  // The kind is remembered across rigs, and another rig may not offer it: the
  // form would then be built from one channel's spec while the browser
  // selected another, and submitting read a field that was never rendered.
  const kind = offered.some(([value]) => value === rigPage.channelKind)
    ? rigPage.channelKind : offered[0][0];
  rigPage.channelKind = kind;
  const spec = CHANNEL_KINDS[kind];
  const fields = spec.fields.map(field => field.options
    ? `<label class="setting"><div><strong>${escapeHtml(field.label)}</strong><small class="muted">${escapeHtml(field.help)}</small></div>`
      + `<span class="field"><select name="${escapeHtml(field.name)}">${field.options.map(option =>
          `<option value="${escapeHtml(option)}">${escapeHtml(option)}</option>`).join("")}</select></span></label>`
    : `<label class="setting"><div><strong>${escapeHtml(field.label)}</strong><small class="muted">${escapeHtml(field.help)}</small></div>`
      + `<span class="field"><input name="${escapeHtml(field.name)}" required${field.pattern ? ` pattern="${escapeHtml(field.pattern)}"` : ""}></span></label>`).join("");
  return `<form id="channel-form" autocomplete="off">
    <label class="setting"><div><strong>Channel</strong><small class="muted">${escapeHtml(spec.how)}</small></div>
      <span class="field"><select name="kind" class="channel-kind">${offered
        .map(([value, item]) =>
        `<option value="${escapeHtml(value)}"${value === kind ? " selected" : ""}>${escapeHtml(item.title)}</option>`).join("")}</select></span></label>
    ${spec.optionalSecret ? `<p class="muted">${escapeHtml(spec.secretHelp)}</p>` : `<label class="setting"><div><strong>${escapeHtml(spec.secret)}</strong><small class="muted">${escapeHtml(spec.secretHelp)}</small></div>
      <span class="field"><input type="password" name="secret" autocomplete="off" spellcheck="false" autocapitalize="off" required></span></label>`}
    ${fields}
    ${spec.optionalSecret ? "" : `<p class="muted">Sealed in this browser to ${escapeHtml(rig.name)}'s key — the portal relays the ciphertext and cannot read it. Check the rig prints this fingerprint (<code>sudo alteriom-hil-admin providers seal-key --fingerprint</code>):</p>
    <p><code class="fingerprint">${escapeHtml(groupedFingerprint(sealKey?.fingerprint))}</code></p>`}
    <div class="settings-footer"><button type="submit"${running ? ' disabled title="It is running a job: drain it and let the run end first"' : ""}>${spec.optionalSecret ? "Use this channel" : "Seal and send"}</button><button type="button" class="secondary channel-cancel">Cancel</button></div>
    <p class="muted">${running ? "It is running a job, and setting a channel restarts its service: drain it and let the run end first. " : ""}${spec.optionalSecret
      ? "Nothing is sent but the choice: the rig notifies through the link it already holds."
      : "The rig decrypts it straight into <code>alteriom-hil-admin notify set</code>."}</p></form>`;
}

function renderRigLogs() {
  const card = $("rig-logs");
  card.hidden = !rigPage.logs;
  if (!rigPage.logs) return;
  if (renderSection(card, `<div class="title-row"><div><p class="eyebrow">SERVICE LOG</p><h2>Logs</h2></div><span class="muted">${escapeHtml(rigPage.logs.at ? `fetched ${shortWhen(rigPage.logs.at)}` : "")}</span></div><pre class="command-log">${escapeHtml(rigPage.logs.text || "")}</pre>`)) {
    const pre = card.querySelector("pre");
    pre.scrollTop = pre.scrollHeight;
  }
}

// What the rig is running now, as the overview's live pipeline shows it.
function renderRigLive(rig) {
  const card = $("rig-live");
  const jobs = lastStatus?.jobs || [];
  const running = (lastQueue.running_jobs || jobs.filter(job => job.status === "running").map(job => job.id))
    .map(id => jobs.find(job => job.id === id))
    .filter(job => job && job.status === "running" && (rig.local || job.worker === rig.name));
  card.hidden = !running.length;
  if (!running.length) return renderSection(card, "");
  if (renderSection(card, liveRunColumn(running[0], running.slice(1), lastInventory))) installJobActionHandlers(card);
  const progress = inferredProgress(running[0]);
  setProgress(card, progress.length ? progress.filter(stage => ["passed", "skipped"].includes(stage.status)).length / progress.length : 0);
}

// What can be started on this rig, on the tab that lists what it has run --
// so "run the health check again" is a button beside the runs rather than
// something to go looking for in the page heading.
function runControls(rig) {
  if (rig.pending) return "";
  const busy = kind => rigPage.busy.has(kind) ? " disabled" : "";
  const buttons = [];
  if (canaryAvailable() && (rig.local || rig.online)) {
    buttons.push(`<button class="secondary rig-canary">Check every board</button>`);
  }
  buttons.push(rig.local
    ? `<button class="secondary rig-rediscover"${rigBusy ? " disabled" : ""}>Rediscover</button>`
    : `<button class="secondary admin-only rig-command" data-kind="rediscover"${busy("rediscover")}>Rediscover</button>`);
  if (!rig.local) {
    buttons.push(rig.drained
      ? `<button class="secondary admin-only rig-resume">Resume</button>`
      : `<button class="secondary admin-only rig-drain">Drain</button>`);
  }
  const note = rig.drained
    ? `<span class="state warn">drained: it takes no new run</span>`
    : Number(rig.running) > 0 ? `<span class="state warn">${escapeHtml(String(rig.running))} running</span>`
    : `<span class="muted">idle</span>`;
  return `<div class="run-controls">${buttons.join("")}${note}</div>`;
}

async function loadRigRuns(name, offset = 0) {
  const worker = shell().workerQuery(name);
  const limit = 20;
  // The same race as the history's: the poll asks for the page it knows about
  // while the operator is clicking Older, and the slower answer wins.
  const asked = rigPage.runsRequest = (rigPage.runsRequest || 0) + 1;
  const current = () => rigPage.name === name && rigPage.runsRequest === asked;
  // The page being asked for, recorded before the asking: a poll already
  // awaiting the rig's detail when Older is clicked would otherwise resume
  // and ask for the page that was clicked away from. See loadRigHistory.
  rigPage.runsOffset = Math.max(0, offset);
  let page;
  try { page = await api(`/api/v1/jobs?limit=${limit}&offset=${Math.max(0, offset)}${worker}`); }
  catch (error) {
    // The same guard as the answer that arrives: a request for the rig that
    // was left, or one already superseded, says nothing about what is shown.
    if (!current()) return;
    renderSection($("rig-runs"), `<p class="failure-summary">${escapeHtml(error.message)}</p>`);
    return;
  }
  if (!current()) return;
  rigPage.runsOffset = page.offset || 0;
  const jobs = page.jobs || [];
  const total = page.total ?? jobs.length;
  const first = jobs.length ? rigPage.runsOffset + 1 : 0;
  const last = rigPage.runsOffset + jobs.length;
  renderSection($("rig-runs"), `<div class="title-row"><div><p class="eyebrow">RUNS</p><h2>Runs on this rig</h2></div><span class="muted">${escapeHtml(String(total))} in all</span></div>${
    jobs.length
      ? `<div class="table-wrap"><table class="fleet"><thead><tr><th>Created</th><th>Project</th><th>Status</th><th class="num">Took</th><th>Result</th></tr></thead><tbody>${jobs.map(job => `<tr class="clickable" data-href="#run/${escapeHtml(job.id)}"><td class="nowrap">${whenSpan(job.created_at)}</td><td><a class="row-link" href="#run/${escapeHtml(job.id)}">${escapeHtml(jobProject(job))}</a></td><td><span class="state ${statusClass(job.status)}">${escapeHtml(job.status)}</span></td><td class="num">${escapeHtml(formatDuration(jobElapsed(job)))}</td><td>${escapeHtml(shortDetail(jobSummary(job), 90))}</td></tr>`).join("")}</tbody></table></div>`
        + `<div class="pager"><span class="muted">${escapeHtml(String(first))}–${escapeHtml(String(last))} of ${escapeHtml(String(total))}</span>`
        + `<button class="secondary runs-page" data-offset="${Math.max(0, rigPage.runsOffset - limit)}"${rigPage.runsOffset ? "" : " disabled"}>Newer</button>`
        + `<button class="secondary runs-page" data-offset="${rigPage.runsOffset + limit}"${last < total ? "" : " disabled"}>Older</button></div>`
      : '<p class="muted">No run on this rig yet.</p>'}`) && linkRows($("rig-runs"));
}

// After a channel change made with a `configure` command -- which is the one
// kind rigCommand leaves the page alone for, so that a settings editor can
// close itself. Whether it was applied or refused, what the card shows next
// is the rig's answer rather than the state it was rendered in.
function finishChannelChange(name, outcome) {
  if (rigPage.name !== name) return;
  rigPage.editing = null;
  // A rig reports the command's result first and its configuration on the
  // next heartbeat (farm_node._report_control_results), so the portal still
  // holds the configuration from before the change: reloading now renders
  // what the rig has already stopped doing -- "off" under a channel that was
  // just turned on. The card says it is waiting rather than saying that.
  // Whose change, as well as when. A bare stamp was compared against whatever
  // rig was open next, and another rig -- which reports on its own schedule,
  // and last reported before this one was changed -- looked like the one with
  // something unreported, its buttons withheld until it happened to report.
  rigPage.channelPending = outcome?.status === "done"
    ? {name, at: outcome.finished_at || new Date().toISOString()} : null;
  forgetRendered($("rig-channels"));
  showRig(name);
}

// Whether what the portal knows about this rig predates a change it has
// already carried out.
function channelChangePending(rig) {
  if (!rigPage.channelPending || rigPage.channelPending.name !== rig.name) return false;
  const reported = Date.parse(rig.config_at || "");
  const changed = Date.parse(rigPage.channelPending.at);
  if (Number.isNaN(changed)) return false;
  if (!Number.isNaN(reported) && reported > changed) {
    rigPage.channelPending = null;  // the rig has reported since
    return false;
  }
  return true;
}

// Ask a rig for something, and follow it until the rig says how it went.
async function rigCommand(name, kind, args = {}) {
  rigPage.busy.add(kind);
  openConsole(name);
  if (rigPage.detail) renderRig();
  try {
    const command = await api(`/api/v1/workers/${encodeURIComponent(name)}/commands`, {method: "POST", body: JSON.stringify({kind, args})});
    const deadline = Date.now() + 5 * 60 * 1000;
    while (Date.now() < deadline) {
      await sleep(2000);
      const page = await api(`/api/v1/workers/${encodeURIComponent(name)}/commands?limit=10`);
      if (rigPage.name === name && rigPage.detail) {
        rigPage.detail.commands = page.commands || [];
        renderSection($("rig-activity"), rigActivity(rigPage.detail));
      }
      const current = (page.commands || []).find(item => item.id === command.id);
      if (current && ["done", "failed", "expired"].includes(current.status)) return current;
    }
    return null;
  } finally {
    rigPage.busy.delete(kind);
    if (rigPage.name === name && kind !== "configure") showRig(name);
  }
}

async function boardCommand(rigName, kind, args, {local}) {
  if (local) {
    if (kind === "read_details") return api(`/api/v1/inventory/${encodeURIComponent(args.board)}/details`);
    if (kind === "register") return api("/api/v1/inventory/register", {method: "POST", body: JSON.stringify({id: args.id, mac: args.mac})});
    if (kind === "unregister") return api(`/api/v1/inventory/${encodeURIComponent(args.id)}`, {method: "DELETE"});
  }
  const outcome = await rigCommand(rigName, kind, args);
  if (!outcome) throw new Error(`${rigName} has not said how it went; see its activity`);
  if (outcome.status !== "done") throw new Error(outcome.detail || `the rig says ${outcome.status}`);
  return outcome.result;
}

// The rig page's controls, installed once: its sections are re-rendered only
// when they change, so handlers bound to their elements would be lost or
// doubled. Each handler reads the rig the page shows now.
function installRigPageHandlers() {
  const page = document.querySelector('.page[data-page="rig"]');
  const current = () => rigPage.detail;
  page.addEventListener("click", async event => {
    const rig = current();
    const target = event.target.closest("button");
    if (!rig || !target || target.disabled) return;
    const name = rig.name;
    const has = className => target.classList.contains(className);
    if (has("rig-canary")) return requestHealth(boardsOf(rig).map(board => board.id), target);
    if (has("rig-rediscover")) return $("refresh").click();
    if (has("active-view")) return showJob(target.dataset.id, {focus: true, force: true});
    if (has("rig-command")) {
      const kind = target.dataset.kind;
      const asks = {
        restart: `Restart the farm service on ${name}? It reconnects within a minute.`,
        update_now: `Install the current release on ${name} now? It finishes its runs first.`,
      };
      if (asks[kind] && !confirm(asks[kind])) return;
      try {
        const outcome = await rigCommand(name, kind, kind === "logs" ? {lines: 300} : {});
        if (kind === "logs" && outcome?.status === "done") {
          rigPage.logs = {text: outcome.result?.log || "", at: outcome.finished_at};
          // The log is rendered into a section that belongs to Setup, and the
          // button that asks for it is in the page heading, on every tab:
          // without this it scrolled to something the tab was hiding.
          goToRigTab("setup");
          renderRigLogs();
          bringIntoView($("rig-logs"));
        } else if (outcome && outcome.status !== "done") {
          alert(`${COMMAND_LABELS[kind] || kind} on ${name}: ${outcome.status}${outcome.detail ? `\n\n${shortDetail(outcome.detail, 600)}` : ""}`);
        }
      } catch (error) { alert(`${COMMAND_LABELS[kind] || kind} failed: ${error.message}`); }
      return refresh(true);
    }
    if (has("rig-drain")) {
      const reason = prompt(`Drain ${name}: it finishes what it runs and is given nothing new until resumed. Why? (optional)`);
      if (reason === null) return;
      try { await api(`/api/v1/workers/${encodeURIComponent(name)}/drain`, {method: "POST", body: JSON.stringify(reason.trim() ? {reason: reason.trim()} : {})}); }
      catch (error) { alert(`Drain failed: ${error.message}`); }
      await refresh(true);
      return showRig(name);
    }
    if (has("rig-resume")) {
      try { await api(`/api/v1/workers/${encodeURIComponent(name)}/resume`, {method: "POST", body: "{}"}); }
      catch (error) { alert(`Resume failed: ${error.message}`); }
      await refresh(true);
      return showRig(name);
    }
    if (has("rig-delete")) {
      const what = rig.pending
        ? `Delete ${name}? It has not joined: its join token stops working${rig.join?.status === "installing" ? " and the key it took is revoked" : ""}, and nothing of it is kept.`
        : `Delete ${name} from the portal? Its key is revoked: to come back it has to be added again. Its runs stay in the history.`;
      if (!rig.pending) {
        if (prompt(`${what}\n\nType ${name} to confirm.`) !== name) return;
      } else if (!confirm(what)) return;
      try {
        await api(`/api/v1/rigs/${encodeURIComponent(name)}`, {method: "DELETE"});
        joinTokens.delete(name);
        await refresh(true);
        navigateTo("#rigs");
      } catch (error) { alert(`Delete failed: ${error.message}`); }
      return;
    }
    if (has("rig-edit")) {
      rigPage.editing = "details";
      // The editor is rendered into the summary, which is Overview's; the
      // button is in the page heading, which is on every tab. A control that
      // is always reachable has to take the operator to what it opens.
      rigPage.tab = "overview";
      forgetRendered($("rig-summary"));
      renderSection($("rig-summary"), rig.pending ? shell().pendingSummary(rig) : rigSummary(rig));
      renderRigTabs(rig);
      applyRigTab();
      $("rig-details-form")?.elements[rig.pending ? "name" : "description"]?.focus();
      return;
    }
    if (has("rig-edit-cancel")) {
      rigPage.editing = null;
      forgetRendered($("rig-summary"));
      return renderRig();
    }
    if (has("rig-join-new")) {
      const revokes = rig.join?.status === "installing" ? " It took its key already: that key is revoked, and the install has to be run again with the new command." : "";
      if (rig.join?.status === "waiting" && !confirm(`Make a new join command for ${name}? The one made before stops working.${revokes}`)) return;
      if (rig.join?.status === "installing" && !confirm(`Make a new join command for ${name}?${revokes}`)) return;
      target.disabled = true;
      try {
        const renewed = await api(`/api/v1/rigs/${encodeURIComponent(name)}/join`, {method: "POST", body: "{}"});
        joinTokens.set(name, {token: renewed.token, expires_at: renewed.join?.expires_at});
        rigPage.detail = renewed;
        renderRig();
      } catch (error) { alert(`Could not make a join command: ${error.message}`); target.disabled = false; }
      return;
    }
    if (has("copy-join")) {
      const text = $("join-command")?.textContent || "";
      try {
        await navigator.clipboard.writeText(text);
        target.textContent = "Copied";
      } catch {
        // No clipboard outside a secure context: select it for the operator.
        const range = document.createRange();
        range.selectNodeContents($("join-command"));
        getSelection().removeAllRanges();
        getSelection().addRange(range);
      }
      return;
    }
    if (has("settings-edit")) {
      rigPage.editing = "settings";
      forgetRendered($("rig-settings"));
      renderSection($("rig-settings"), rigSettings(rig));
      return;
    }
    if (has("settings-cancel")) {
      rigPage.editing = null;
      forgetRendered($("rig-settings"));
      return renderRig();
    }
    // The tab bar only: every section carries `data-tab` now, so anything but
    // a tab button would otherwise change tab when clicked.
    const tab = target.closest?.("#rig-tabs .tab");
    if (tab) return goToRigTab(tab.dataset.tab);
    const goTo = target.closest?.(".rig-tab-go");
    if (goTo) return goToRigTab(goTo.dataset.go);
    const historyPage = target.closest?.(".history-page");
    if (historyPage) return loadRigHistory(name, Number(historyPage.dataset.offset) || 0);
    const runsPage = target.closest?.(".runs-page");
    if (runsPage) return loadRigRuns(name, Number(runsPage.dataset.offset) || 0);
    if (has("setup-toggle")) {
      rigPage.setupOpen = !rigPage.setupOpen;
      forgetRendered($("rig-setup"));
      renderSection($("rig-setup"), rigSetup(rig));
      return;
    }
    const visibility = target.closest?.(".rig-visibility");
    if (visibility) {
      return changeVisibility(rig, visibility.dataset.visibility);
    }
    const capability = target.closest?.(".rig-capability");
    if (capability) {
      // The chip is in the heading, on every tab; the row it stands for is
      // in Setup's table, behind Details. Open both, then go to the row.
      rigPage.setupOpen = true;
      goToRigTab("setup");
      const row = $(`setup-row-${capability.dataset.key}`);
      if (row) bringIntoView(row);
      return;
    }
    if (has("channel-add")) {
      rigPage.editing = "channel-add";
      rigPage.channelKind = rigPage.channelKind || (rig.config?.notify?.channel) || "telegram";
      forgetRendered($("rig-channels"));
      renderSection($("rig-channels"), rigChannels(rig));
      $("rig-channels").querySelector('input[name="secret"]')?.focus();
      return;
    }
    if (has("channel-cancel")) {
      const input = $("rig-channels").querySelector('input[name="secret"]');
      if (input) input.value = "";
      rigPage.editing = null;
      forgetRendered($("rig-channels"));
      return renderRig();
    }
    if (has("channel-test")) {
      try {
        // Which channel: a rig can hold several, and "send a test" says
        // nothing about which one is being tested.
        const outcome = await rigCommand(name, "notify_test",
                                         {settings: {id: target.dataset.id || "1"}});
        if (!outcome) alert(`${name} has not said how it went yet; see its activity.`);
        else alert(`Test message on ${name}: ${outcome.status === "done" ? "" : `${outcome.status}\n\n`}${shortDetail(outcome.detail || outcome.status, 600)}`);
      } catch (error) { alert(`Test message refused: ${error.message}`); }
      return;
    }
    if (has("channel-off")) {
      if (!confirm(`Stop ${name} sending down this channel? Its credential file is left in place, so it can be turned back on.`)) return;
      try {
        const outcome = await rigCommand(name, "notify_tune",
                                         {settings: {id: target.dataset.id || "1", enabled: false}});
        if (outcome && outcome.status !== "done") alert(`Turn off on ${name}: ${outcome.status}${outcome.detail ? `\n\n${shortDetail(outcome.detail, 600)}` : ""}`);
      } catch (error) { alert(`Refused: ${error.message}`); }
      return;
    }
    if (has("channel-on")) {
      // The credential is still in its file: turning it back on is a setting,
      // not another sealed credential.
      let outcome = null;
      try {
        outcome = await rigCommand(name, "notify_tune",
                                   {settings: {id: target.dataset.id || "1", enabled: true}});
        if (outcome && outcome.status !== "done") alert(`Turn on for ${name}: ${outcome.status}${outcome.detail ? `\n\n${shortDetail(outcome.detail, 600)}` : ""}`);
      } catch (error) { alert(`Refused: ${error.message}`); }
      // A `configure` is the one command rigCommand does not reload the page
      // after -- the settings form does it itself, because it has an editor to
      // close. These have one too, and without this the card kept the state it
      // was rendered with: turned on and still saying off, with its buttons
      // disabled, until some later poll.
      finishChannelChange(name, outcome);
      return;
    }
    if (has("channel-events")) {
      rigPage.editing = "channel-events";
      rigPage.channelEditing = target.dataset.id || "1";
      forgetRendered($("rig-channels"));
      renderSection($("rig-channels"), rigChannels(rig));
      return;
    }
    if (has("provider-set-open") || has("provider-policy-open")) {
      rigPage.editing = has("provider-set-open") ? "provider" : "provider-policy";
      forgetRendered($("rig-channels"));
      renderSection($("rig-channels"), rigChannels(rig));
      $("rig-channels").querySelector('input[name="link"]')?.focus();
      return;
    }
    if (has("provider-cancel")) {
      const input = $("rig-channels").querySelector('input[name="link"]');
      if (input) input.value = "";
      rigPage.editing = null;
      forgetRendered($("rig-channels"));
      return renderRig();
    }
    if (has("channel-details")) {
      // One card's details at a time, in the card: the rows and the buttons
      // that change a channel were a separate section further down the page.
      const key = target.dataset.channel;
      rigPage.channelOpen = rigPage.channelOpen === key ? null : key;
      forgetRendered($("rig-channels"));
      renderSection($("rig-channels"), rigChannels(rig));
      return;
    }
    if (has("provider-remove")) {
      if (!confirm(`Remove the CallMeBot link stored on ${name}? Its CallMeBot row skips until a link is set again, and its service restarts.`)) return;
      try {
        const outcome = await rigCommand(name, "provider_remove", {provider: "callmebot"});
        if (outcome && outcome.status !== "done") alert(`Remove link on ${name}: ${outcome.status}${outcome.detail ? `\n\n${shortDetail(outcome.detail, 600)}` : ""}`);
      } catch (error) { alert(`Remove link refused: ${error.message}`); }
      return;
    }
    if (has("provider-test")) {
      const callmebot = rig.config?.callmebot || {};
      if (!confirm(`Send one WhatsApp to the number stored on ${name}? It counts against today's budget (${callmebot.used_today ?? 0}/${callmebot.max_per_day ?? "?"}).`)) return;
      try {
        const outcome = await rigCommand(name, "provider_test", {provider: "callmebot"});
        if (!outcome) alert(`${name} has not said how it went yet; see its activity.`);
        else alert(`Test message on ${name}: ${outcome.status === "done" ? "" : `${outcome.status}\n\n`}${shortDetail(outcome.detail || outcome.status, 600)}`);
      } catch (error) { alert(`Test message refused: ${error.message}`); }
      return;
    }
    if (has("unregister-board")) {
      const id = target.dataset.id;
      if (!confirm(`Remove ${id} from ${rigLabel(rig)}'s registry?`)) return;
      target.disabled = true;
      try { await boardCommand(name, "unregister", {id}, {local: rig.local}); await refresh(true); showRig(name); }
      catch (error) { alert(`Unregister failed: ${error.message}`); target.disabled = false; }
    }
  });
  page.addEventListener("input", event => {
    const form = event.target.closest("#rig-settings-form");
    if (!form) return;
    let changed = 0;
    for (const input of form.querySelectorAll("input")) {
      const differs = input.type === "checkbox" ? input.checked !== (input.dataset.was === "1") : input.value !== input.dataset.was;
      input.closest(".setting")?.classList.toggle("changed", differs);
      if (differs) changed += 1;
    }
    $("settings-changes").textContent = changed ? `${changed} change${changed === 1 ? "" : "s"}` : "No changes yet";
  });
  page.addEventListener("submit", async event => {
    const rig = current();
    const form = event.target;
    if (!rig) return;
    const name = rig.name;
    if (form.classList.contains("register-board")) {
      event.preventDefault();
      const button = form.querySelector("button"); button.disabled = true; button.textContent = "Registering…";
      try { await boardCommand(name, "register", {id: new FormData(form).get("id"), mac: form.dataset.mac}, {local: rig.local}); await refresh(true); showRig(name); }
      catch (error) { alert(`Registration failed: ${error.message}`); button.disabled = false; button.textContent = "Register"; }
      return;
    }
    if (form.id === "rig-details-form") {
      event.preventDefault();
      const change = {description: form.elements.description.value.trim() || null, location: form.elements.location.value.trim() || null};
      if (form.elements.name && form.elements.name.value.trim() !== name) change.name = form.elements.name.value.trim();
      const button = form.querySelector("button[type=submit]"); button.disabled = true;
      try {
        const saved = await api(`/api/v1/rigs/${encodeURIComponent(name)}`, {method: "PATCH", body: JSON.stringify(change)});
        if (saved.name !== name && joinTokens.has(name)) { joinTokens.set(saved.name, joinTokens.get(name)); joinTokens.delete(name); }
        rigPage.editing = null;
        forgetRendered($("rig-summary"));
        await refresh(true);
        if (saved.name !== name) return navigateTo(rigHref(saved.name));
        rigPage.detail = saved;
        renderRig();
      } catch (error) { alert(`Could not save: ${error.message}`); button.disabled = false; }
      return;
    }
    if (form.id === "channel-events-form") {
      event.preventDefault();
      const chosen = Object.keys(NOTIFY_EVENTS).filter(key => form.elements[key]?.checked);
      if (!chosen.length) return alert("Not sent: choose at least one, or turn the channel off.");
      rigPage.editing = null;
      forgetRendered($("rig-channels"));
      let outcome = null;
      try {
        outcome = await rigCommand(name, "notify_tune",
          {settings: {id: rigPage.channelEditing || "1", events: chosen.join(",")}});
        if (outcome && outcome.status !== "done") {
          alert(`Events on ${name}: ${outcome.status}${outcome.detail ? `\n\n${shortDetail(outcome.detail, 600)}` : ""}`);
        }
      } catch (error) { alert(`Refused: ${error.message}`); }
      finishChannelChange(name, outcome);
      return;
    }
    if (form.id === "channel-form") {
      event.preventDefault();
      const kind = form.elements.kind.value;
      if (CHANNEL_KINDS[kind]?.optionalSecret) {
        // The rig's own link: nothing to seal, only which channel to use.
        rigPage.editing = null;
        forgetRendered($("rig-channels"));
        try {
          const outcome = await rigCommand(name, "notify_set", {channel: kind, settings: {}});
          if (outcome && outcome.status !== "done") {
            alert(`Set channel on ${name}: ${outcome.status}${outcome.detail ? `

${shortDetail(outcome.detail, 600)}` : ""}`);
          }
        } catch (error) { alert(`Refused: ${error.message}`); }
        return;
      }
      const input = form.elements.secret;
      // Taken and cleared at once: nothing of the credential stays in the
      // page, and it is never logged, stored or sent as it is.
      let secret = input.value.trim();
      input.value = "";
      if (!window.isSecureContext || !globalThis.crypto?.subtle) {
        secret = "";
        return alert("This page is not a secure context (https, or localhost): the browser cannot seal it here.");
      }
      const settings = {};
      (CHANNEL_KINDS[kind]?.fields || []).forEach(field => {
        const value = (form.elements[field.name]?.value || "").trim();
        if (value) settings[field.name] = value;
      });
      if (kind === "telegram" && !/^-?[0-9]{1,20}$/.test(settings.chat_id || "")) {
        secret = "";
        return alert("Not sent: the chat id is a number, like 12345678 or -1001234567890.");
      }
      if (kind === "webhook" && !/^https:\/\//.test(secret)) {
        secret = "";
        return alert("Not sent: the webhook URL must be https.");
      }
      const sealKey = rig.config?.seal_key;
      if (!sealKey?.spki || !sealKey?.fingerprint) { secret = ""; return alert(`${name} has not reported a seal key; reload the page.`); }
      const button = form.querySelector("button[type=submit]"); button.disabled = true; button.textContent = "Sealing…";
      let sealed;
      try { sealed = await sealProviderLink(sealKey.spki, secret); }
      catch (error) {
        alert(`The browser could not seal it to ${name}'s key (${error.name || "error"}); reload the page.`);
        button.disabled = false; button.textContent = "Seal and send";
        return;
      } finally { secret = ""; }
      if (sealed.fingerprint !== sealKey.fingerprint) {
        button.disabled = false; button.textContent = "Seal and send";
        return alert(`The key ${name} reported does not match its fingerprint; nothing was sent. Reload the page.`);
      }
      rigPage.editing = null;
      forgetRendered($("rig-channels"));
      try {
        const outcome = await rigCommand(name, "notify_set", {channel: kind, settings, ...sealed});
        if (outcome && outcome.status !== "done") {
          alert(`Set channel on ${name}: ${outcome.status}${outcome.detail ? `\n\n${shortDetail(outcome.detail, 600)}` : ""}`);
        }
      } catch (error) { alert(`Refused: ${error.message}`); }
      return;
    }
    if (form.id === "provider-link-form") {
      event.preventDefault();
      const input = form.elements.link;
      // Taken and cleared at once: nothing of the link stays in the page, and
      // it is never logged, stored or sent as it is.
      let link = input.value.trim();
      input.value = "";
      if (!window.isSecureContext || !globalThis.crypto?.subtle) {
        link = "";
        return alert("This page is not a secure context (https, or localhost): the browser cannot seal the link here.");
      }
      const problem = callmebotLinkProblem(link);
      if (problem) { link = ""; return alert(`Not sent: ${problem}.`); }
      const sealKey = rig.config?.seal_key;
      if (!sealKey?.spki || !sealKey?.fingerprint) { link = ""; return alert(`${name} has not reported a seal key; reload the page.`); }
      const button = form.querySelector("button[type=submit]"); button.disabled = true; button.textContent = "Sealing…";
      let sealed;
      try { sealed = await sealProviderLink(sealKey.spki, link); }
      catch (error) {
        alert(`The browser could not seal the link to ${name}'s key (${error.name || "error"}); reload the page.`);
        button.disabled = false; button.textContent = "Seal and send";
        return;
      } finally { link = ""; }
      if (sealed.fingerprint !== sealKey.fingerprint) {
        button.disabled = false; button.textContent = "Seal and send";
        return alert(`The key ${name} reported does not match its fingerprint; nothing was sent. Reload the page.`);
      }
      rigPage.editing = null;
      forgetRendered($("rig-channels"));
      try {
        const outcome = await rigCommand(name, "provider_set", {provider: "callmebot", sealed: sealed.sealed, fingerprint: sealed.fingerprint});
        if (!outcome) alert(`${name} has not said how it went yet; see its activity.`);
        else if (outcome.status !== "done") alert(`${name} did not store the link: ${shortDetail(outcome.detail || outcome.status, 600)}`);
      } catch (error) { alert(`Set link refused: ${error.message}`); }
      return;
    }
    if (form.id === "provider-policy-form") {
      event.preventDefault();
      const settings = {};
      for (const field of form.querySelectorAll("select, input")) {
        if (field.value === "" || field.value === field.dataset.was) continue;
        settings[field.name] = field.tagName === "SELECT" ? field.value : Number(field.value);
      }
      if (!Object.keys(settings).length) return alert("Nothing was changed.");
      const labels = Object.fromEntries(PROVIDER_FIELDS.map(field => [field.key, field.label]));
      if (!confirm(`Apply on ${name}?\n\n${Object.entries(settings).map(([key, value]) => `${labels[key] || key}: ${value}`).join("\n")}\n\nThe rig restarts its service to take them.`)) return;
      const button = form.querySelector("button[type=submit]"); button.disabled = true; button.textContent = "Applying…";
      try {
        const outcome = await rigCommand(name, "configure", {settings});
        if (outcome?.status !== "done") {
          alert(`The rig did not apply them: ${outcome ? shortDetail(outcome.detail, 600) : "no answer yet"}`);
          button.disabled = false; button.textContent = `Apply on ${name}`;
          return;
        }
        rigPage.editing = null;
        forgetRendered($("rig-channels"));
        showRig(name);
      } catch (error) { alert(`Change refused: ${error.message}`); button.disabled = false; button.textContent = `Apply on ${name}`; }
      return;
    }
    if (form.id === "rig-settings-form") {
      event.preventDefault();
      const settings = {};
      for (const input of form.querySelectorAll("input")) {
        if (input.type === "checkbox") {
          if (input.checked !== (input.dataset.was === "1")) settings[input.name] = input.checked;
        } else if (input.value !== "" && input.value !== input.dataset.was) {
          settings[input.name] = Number(input.value);
        }
      }
      if (!Object.keys(settings).length) return alert("Nothing was changed.");
      const labels = Object.fromEntries(REMOTE_FIELDS.map(field => [field.key, field.label]));
      if (!confirm(`Apply on ${name}?\n\n${Object.entries(settings).map(([key, value]) => `${labels[key] || key}: ${value}`).join("\n")}\n\nThe rig restarts its service to take them.`)) return;
      const button = form.querySelector("button[type=submit]"); button.disabled = true; button.textContent = "Applying…";
      try {
        const outcome = await rigCommand(name, "configure", {settings});
        if (outcome?.status !== "done") {
          alert(`The rig did not apply them: ${outcome ? shortDetail(outcome.detail, 600) : "no answer yet"}`);
          button.disabled = false; button.textContent = `Apply on ${name}`;
          return;
        }
        rigPage.editing = null;
        forgetRendered($("rig-settings"));
        showRig(name);
      } catch (error) { alert(`Change refused: ${error.message}`); button.disabled = false; button.textContent = `Apply on ${name}`; }
    }
  });
}

// ---- A board's page -------------------------------------------------------------------
async function showBoard(id) {
  if (boardPage.id !== id) Object.assign(boardPage, {id, history: null});
  renderBoard();
  try { boardPage.history = await api(`/api/v1/inventory/${encodeURIComponent(id)}/history`); }
  catch (error) { boardPage.history = {error: error.message}; }
  if (boardPage.id === id) renderBoard();
}

function renderBoard() {
  const id = boardPage.id;
  if (!id) return;
  const inv = lastInventory || {};
  const board = (inv.boards || []).find(item => item.id === id);
  const missing = (inv.missing || []).includes(id);
  const rigName = shell().boardRigName(board);
  boardPage.rig = rigName;
  const state = board ? boardState(board) : {label: missing ? "MISSING" : "UNKNOWN", tone: "warn"};
  $("board-title").textContent = id;
  $("board-subtitle").innerHTML = [`<span class="state ${state.tone}">${escapeHtml(state.label)}</span>`, board ? healthBadge(board) : "", board ? escapeHtml(familyLabel(board.target)) : "", rigName ? `on <a href="${rigHref(rigName)}">${escapeHtml(rigName === "local" ? rigLabel(localRig()) : rigName)}</a>` : ""].filter(Boolean).join(" ");
  const hold = board ? holdFacts(board) : {note: "", control: ""};
  // Reading a chip opens its serial port: never under the run using it, and
  // on a farm host never beside a run that has the whole rig to itself.
  const readable = board && shell().boardReadable(state, rigAlone);
  $("board-actions").innerHTML = board
    ? `${canaryAvailable() ? '<button class="secondary board-health">Check health</button>' : ""}<button class="secondary admin-only board-read"${readable ? "" : ' disabled title="A run is using it: its serial port is the run\'s"'}>Read chip</button>${hold.control}`
    : "";
  if (!board) {
    $("board-summary").innerHTML = missing
      ? `<p class="muted">${escapeHtml(id)} is registered and not reported by its rig now: unplugged, or on a rig that stopped answering.</p>`
      : `<p class="failure-summary">No board ${escapeHtml(id)} is known.</p><p class="muted"><a href="#boards">All boards</a></p>`;
  } else {
    const rows = [
      ["Rig", rigName ? `<a href="${rigHref(rigName)}">${escapeHtml(rigName === "local" ? rigLabel(localRig()) : rigName)}</a>` : "—"],
      ["Family", escapeHtml(board.target)],
      ["MAC", `<code>${escapeHtml(board.mac || "")}</code>`],
      ["Port", escapeHtml(board.port || "")],
      ["USB path", escapeHtml(board.usb_path || "")],
      ["Tags", escapeHtml((board.tags || []).join(", ") || "—")],
      ["Power switching", board.power_hub ? escapeHtml(`hub ${board.power_hub} port ${board.power_port}`) : '<span class="muted">none</span>'],
    ];
    $("board-summary").innerHTML = `<div class="detail-grid">${rows.map(([label, value]) => `<div><small>${label}</small><span>${value}</span></div>`).join("")}</div>${hold.note}${board.held_by ? `<p class="muted">Held by <a href="#run/${escapeHtml(board.held_by.job_id)}">run ${escapeHtml((board.held_by.job_id || "").slice(0, 8))}</a></p>` : ""}`;
  }
  $("board-chip").innerHTML = `<div class="title-row"><div><p class="eyebrow">SILICON</p><h2>Chip</h2></div></div>${board ? renderDetails(board) : '<p class="muted">Not connected.</p>'}`;
  const health = board?.health;
  const checks = Object.entries(health?.checks || {});
  $("board-canary").innerHTML = `<div class="title-row"><div><p class="eyebrow">RIG HEALTH CHECK${health?.canary_version ? ` ${escapeHtml(health.canary_version)}` : ""}</p><h2>Last health check</h2></div>${health?.job_id ? `<a href="#run/${escapeHtml(health.job_id)}">the run</a>` : ""}</div>${
    health
      ? `<p>${healthBadge(board)} <span class="muted">${escapeHtml(shortWhen(health.checked_at))}</span></p>${checks.length ? `<div class="table-wrap"><table class="compact check-table"><tbody>${checks.map(([check, verdict]) => `<tr><td>${escapeHtml(check.replace(/^test_/, "").replace(/_/g, " "))}</td><td><span class="state ${verdict === "passed" ? "good" : verdict === "skipped" ? "muted" : "bad"}">${escapeHtml(verdict)}</span></td></tr>`).join("")}</tbody></table></div>` : ""}${(health.farm_wide || []).length ? `<p class="failure-summary">${escapeHtml(checkNames(health.farm_wide))} failed on every board: the farm, not this board.</p>` : ""}`
      : '<p class="muted">The Rig Health Check has not checked this board yet.</p>'}`;
  const history = boardPage.history;
  const verdicts = history?.verdicts || [];
  $("board-history").innerHTML = `<div class="title-row"><div><p class="eyebrow">HISTORY</p><h2>Health check verdicts</h2></div><span class="muted">newest first</span></div>${
    history?.error ? `<p class="failure-summary">${escapeHtml(history.error)}</p>`
    : !history ? '<p class="muted">Loading…</p>'
    : verdicts.length
      ? `<div class="table-wrap"><table class="fleet"><thead><tr><th>Checked</th><th>Verdict</th><th>Whose fault</th><th>Failed checks</th><th>Run</th></tr></thead><tbody>${verdicts.map(row => {
          const failed = row.failed || (row.failed_json ? JSON.parse(row.failed_json) : []);
          return `<tr><td class="nowrap">${whenSpan(row.checked_at)}</td><td><span class="state ${row.verdict === "passed" || row.verdict === "healthy" ? "good" : "bad"}">${escapeHtml(row.verdict)}</span></td><td>${escapeHtml(row.outcome || "")}</td><td>${escapeHtml(checkNames(failed) || "—")}</td><td>${row.job_id
            ? `<a href="#run/${escapeHtml(row.job_id)}">${escapeHtml(String(row.job_id).slice(0, 8))}</a>`
            // The verdict is the board's and is shown; the run that found it
            // out is not this caller's, so board_history sends no id. Linking
            // it anyway rendered a "null" pointing at #run/ -- a dead link
            // offering to open something they may not open.
            : '<span class="muted" title="a run on this rig that is not yours to open">—</span>'}</td></tr>`;
        }).join("")}</tbody></table></div>`
      : '<p class="muted">No Rig Health Check verdict recorded for this board.</p>'}`;
  const page = document.querySelector('.page[data-page="board"]');
  page.querySelectorAll(".board-health").forEach(button => button.addEventListener("click", () => requestHealth([id], button)));
  page.querySelectorAll(".board-read").forEach(button => button.addEventListener("click", async () => {
    button.disabled = true; button.textContent = "Reading…";
    try { await boardCommand(rigName, "read_details", {board: id}, {local: shell().boardCommandLocal}); await refresh(true); }
    catch (error) { alert(`Reading the chip failed: ${error.message}`); }
    finally { if (boardPage.id === id) renderBoard(); }
  }));
  installHoldHandlers(page);
}

function installHoldHandlers(root) {
  root.querySelectorAll(".board-reserve").forEach(button => button.addEventListener("click", async () => {
    const id = button.dataset.id;
    const reason = prompt(`Reserve ${id}: nothing is allocated it until it is released. Why? (optional)`);
    if (reason === null) return;
    button.disabled = true;
    try { await api(`/api/v1/inventory/${encodeURIComponent(id)}/reserve`, {method: "POST", body: JSON.stringify(reason.trim() ? {reason: reason.trim()} : {})}); await refresh(true); }
    catch (error) { alert(`Reserve failed: ${error.message}`); button.disabled = false; }
  }));
  root.querySelectorAll(".board-release").forEach(button => button.addEventListener("click", async () => {
    const id = button.dataset.id; if (!confirm(`Put ${id} back in the pool?`)) return; button.disabled = true;
    try { await api(`/api/v1/inventory/${encodeURIComponent(id)}/release`, {method: "POST", body: "{}"}); await refresh(true); }
    catch (error) { alert(`Release failed: ${error.message}`); button.disabled = false; }
  }));
}

// ---- Runs list -------------------------------------------------------------
// The run list is filtered and paged by the service, not in the browser: the
// history only grows, so a client that fetched everything to filter it would
// get slower every week and would quietly stop showing older runs once it hit
// the fetch cap — exactly when an operator is looking for something old.
const runQuery = {q: "", kind: "", status: "", offset: 0, limit: 25};
const STATUS_FILTERS = [["", "All"], ["passed", "Passed"], ["failed", "Failed"], ["cancelled", "Cancelled"], ["running", "Running"], ["queued", "Queued"]];

function renderStatusChips(counts) {
  const total = Object.values(counts || {}).reduce((sum, n) => sum + n, 0);
  $("job-status-filters").innerHTML = STATUS_FILTERS.map(([value, label]) => {
    const n = value ? (counts?.[value] || 0) : total;
    const active = runQuery.status === value ? " active" : "";
    return `<button class="chip${active}" data-status="${escapeHtml(value)}">${escapeHtml(label)} <small>${escapeHtml(n)}</small></button>`;
  }).join("");
  document.querySelectorAll("#job-status-filters .chip").forEach(chip =>
    chip.addEventListener("click", () => { runQuery.status = chip.dataset.status; runQuery.offset = 0; loadJobs(); }));
}

// Both long lists are paged by the service and say the same things about
// where you are: which slice is on screen, out of how many, and whether
// that count is the whole history or what a filter matched. One function so
// the two cannot drift into describing themselves differently.
function renderPager({range, prev, next, offset, shown, matched, noun, filtered}) {
  const first = matched ? offset + 1 : 0;
  const last = offset + shown;
  $(range).textContent = matched
    ? `${first}–${last} of ${matched}${filtered ? ` matching` : ""}`
    : `no ${noun}s${filtered ? " match this filter" : ""}`;
  $(prev).disabled = offset <= 0;
  // Disabled on the last page rather than hidden: a button that moves is
  // harder to use than one that is plainly spent.
  $(next).disabled = last >= matched;
}

function renderJobs(page) {
  const jobs = page.jobs || [];
  // The result column: the summary once there is one, the failure otherwise,
  // the current stage while it runs. What is being validated is in its own
  // columns now -- project, branch, revision, targets -- so it is not
  // repeated here.
  const described = job => {
    if (job.result?.summary || job.status === "failed") return escapeHtml(jobSummary(job));
    if (job.kind === "inventory") return escapeHtml([...(job.progress || [])].reverse().find(stage => stage.summary)?.summary || "Hardware discovery");
    const current = (job.progress || []).find(stage => stage.status === "running");
    return escapeHtml(current ? current.label : job.status === "queued" ? "waiting in queue" : "");
  };
  const queueOrder = lastQueue.queued || [];
  const dash = "—";
  promotableOnlyList = true;
  $("jobs").innerHTML = jobs.map(job => `<tr><td>${new Date(job.created_at).toLocaleString()}</td><td>${escapeHtml(jobProject(job))}${(job.request?.tests?.length || job.request?.keyword) ? ' <span class="state warn" title="A partial run: selected tests only">partial</span>' : ""}${job.worker ? `<small class="sub" title="${job.request?.imported_from ? "Run before this node joined the portal, and brought with its history" : "The node that ran it"}">on ${escapeHtml(job.worker)}</small>` : ""}</td><td>${escapeHtml(jobBranch(job) || dash)}</td><td>${job.kind === "inventory" ? dash : `${jobVersion(job) ? `<strong>${escapeHtml(jobVersion(job))}</strong> · ` : ""}${shaLink(jobRevision(job), jobRepo(job)) || dash}`}</td><td>${escapeHtml(jobTargets(job) || dash)}</td><td><span class="state ${statusClass(job.status)}">${escapeHtml(job.status)}</span></td><td class="duration" data-started="${escapeHtml(job.started_at || "")}" data-final="${job.duration_seconds ?? ""}">${escapeHtml(formatDuration(jobElapsed(job)))}</td><td>${described(job)}</td><td><div class="actions"><button class="view-job secondary" data-id="${escapeHtml(job.id)}">View</button>${jobActionButtons(job, {promotable: queueOrder.indexOf(job.id) > 0})}</div></td></tr>`).join("");
  promotableOnlyList = false
    || `<tr><td colspan="9" class="muted">${runQuery.q || runQuery.status || runQuery.kind ? "No run matches this filter." : "No pipeline history yet."}</td></tr>`;
  document.querySelectorAll(".view-job").forEach(button => button.addEventListener("click", () => {
    openRun(button.dataset.id);
    showJob(button.dataset.id, {force: true});
  }));
  installJobActionHandlers($("jobs"));
  renderPager({
    range: "job-range", prev: "job-prev", next: "job-next",
    offset: page.offset ?? 0, shown: jobs.length,
    matched: page.total ?? jobs.length, noun: "run",
    filtered: Boolean(runQuery.q || runQuery.kind || runQuery.status),
  });
  renderStatusChips(page.counts);
}

async function loadJobs() {
  const params = new URLSearchParams({limit: runQuery.limit, offset: runQuery.offset});
  if (runQuery.q) params.set("q", runQuery.q);
  if (runQuery.kind) params.set("kind", runQuery.kind);
  if (runQuery.status) params.set("status", runQuery.status);
  try { renderJobs(await api(`/api/v1/jobs?${params}`)); }
  catch (error) { $("jobs").innerHTML = `<tr><td colspan="6" class="failure-summary">${escapeHtml(error.message)}</td></tr>`; }
}

// ---- Artifacts -------------------------------------------------------------
// Every firmware bundle the farm keeps on disk: what it is, which runs used
// it, what it takes, and a way to keep it (pin) or let it go (delete, prune).
// Loaded when the tab is opened and after each action, not on every poll:
// the list only changes when a bundle arrives or someone cleans up, and the
// storage figures walk every bundle and every run's evidence on the Pi.
let artifactIndex = null;
let bundleProfileSignature = "";
// Paged and filtered by the service, like the run history: the store only
// grows, so a client that fetched every bundle to filter it here would get
// slower every week and then quietly stop showing the oldest -- which is
// when somebody is looking for an old one.
const bundleQuery = {q: "", profile: "", branch: "", offset: 0, limit: 25};
// Firmware has three views: the library -- each project's branches and their
// latest build -- every build as a list, and the disk with its clean-up.
let firmwareTab = "library";
let lastLibrary = null;

// A bundle's files come through the same fetch-with-token path as a run's.
async function saveFromApi(path, fallbackName) {
  return saveResponse(await fetchFromApi(path), fallbackName);
}

async function viewFromApi(path) {
  return viewResponse(await fetchFromApi(path));
}

function bundleRevision(entry) {
  const sha = entry.revision ? shaLink(entry.revision, entry.repo) : "";
  const named = entry.branch || (entry.ref && !/^[0-9a-f]{7,40}$/i.test(entry.ref) ? entry.ref : "");
  if (!sha) return escapeHtml(named || "—");
  if (entry.version) return `<strong>${escapeHtml(entry.version)}</strong> · ${sha}`;
  return named ? `<span class="branch">${escapeHtml(named)}</span> at ${sha}` : sha;
}

function bundleFamilies(entry) {
  const chips = (entry.families || []).map(family => `<span class="artifact" title="${escapeHtml(family.environment || family.family)}${family.image_bytes ? ` · ${escapeHtml(formatBytes(family.image_bytes))}` : ""}">${escapeHtml(familyLabel(family.family))}</span>`).join("");
  return chips ? `<div class="family-chips">${chips}</div>` : '<span class="muted">none</span>';
}

function shortWhen(value) {
  return value ? new Date(value).toLocaleString([], {dateStyle: "short", timeStyle: "short"}) : "—";
}

// "3h ago" in a list, where a full timestamp on every row is most of the
// row; the exact time is in the tooltip. Older than a week reads as a date.
function relativeWhen(value) {
  if (!value) return "—";
  const seconds = (Date.now() - Date.parse(value)) / 1000;
  if (!Number.isFinite(seconds)) return "—";
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 7 * 86400) return `${Math.floor(seconds / 86400)}d ago`;
  return new Date(value).toLocaleDateString([], {day: "numeric", month: "short", year: "numeric"});
}

function whenSpan(value) {
  return `<span title="${escapeHtml(value ? new Date(value).toLocaleString() : "")}">${escapeHtml(relativeWhen(value))}</span>`;
}

function bundleBadges(entry) {
  return `${entry.imported_from ? ` <span class="state muted" title="Brought with ${escapeHtml(entry.imported_from)}'s history when it joined the portal: shown and downloadable, never chosen to flash a new run">from ${escapeHtml(entry.imported_from)}</span>` : ""}${entry.pinned ? ` <span class="state good" title="${escapeHtml(entry.pin_note || "Kept on purpose")}">pinned</span>` : ""}${entry.held ? ' <span class="state warn" title="A queued or running job is using this bundle">in use</span>' : ""}${entry.manifest_valid ? "" : ' <span class="state bad" title="manifest.json is missing or unreadable">no manifest</span>'}`;
}

// Who started the run a bundle or a job came from, as GitHub names them. A
// bot account has no profile page worth linking, so it is just its name.
function actorLink(login) {
  if (!login) return "";
  if (/\[bot\]$/.test(login)) return `<span title="An app's bot account">${escapeHtml(login)}</span>`;
  return `<a href="https://github.com/${encodeURIComponent(login)}" target="_blank" rel="noopener">${escapeHtml(login)}</a>`;
}

function producerRunLink(source) {
  if (!source?.run_id) return "";
  const label = `CI run ${escapeHtml(source.run_id)}`;
  return /^https:\/\//.test(source.run_url || "") ? `<a href="${escapeHtml(source.run_url)}" target="_blank" rel="noopener">${label}</a>` : label;
}

// Where a bundle came from. Nothing has built on the rig since artifact-first
// step 5, so almost every bundle is supplied -- and "supplied by whom" is the
// question, which the short form leaves to its tooltip and the page answers.
function bundleSource(entry, {long = false} = {}) {
  const source = entry.source || {};
  if (source.kind === "supplied") {
    if (long) return `Supplied by ${source.repo ? repoLink(source.repo) : "a producer"}${source.run_id ? ` · ${producerRunLink(source)}` : ""}`;
    return `<span class="state good" title="Built by ${escapeHtml(source.repo || "a producer")} CI and verified on arrival">supplied</span>`;
  }
  return long ? "Built on the farm, before it stopped building" : '<span class="state" title="Compiled on the rig, before the farm stopped building">farm build</span>';
}

function bundleRuns(entry) {
  return (entry.built_by ? 1 : 0) + (entry.reused_by || []).length;
}

function renderBundleProfiles(profileNames) {
  const names = profileNames || [];
  const signature = JSON.stringify(names);
  if (signature === bundleProfileSignature) return;
  bundleProfileSignature = signature;
  const select = $("bundle-profile");
  const chosen = names.includes(bundleQuery.profile) ? bundleQuery.profile : names.includes(select.value) ? select.value : "";
  select.innerHTML = `<option value="">All projects</option>${names.map(name => `<option value="${escapeHtml(name)}"${name === chosen ? " selected" : ""}>${escapeHtml(profiles[name]?.label || name)}</option>`).join("")}`;
  bundleQuery.profile = chosen;
}

// One row a bundle, and the row is the way in. Four buttons on every row made
// a list of forty bundles a wall of Pin and Delete; they are on the bundle's
// page now, where the decision is made with its details in front of you.
function renderBundles() {
  const index = artifactIndex || {bundles: []};
  // The names come from the whole store, not from this page: a filter whose
  // options depended on what happened to be on screen could not be undone.
  renderBundleProfiles(index.profiles);
  const shown = index.bundles || [];
  // The totals are the disk story and are of the whole store -- a page of
  // five saying "5 bundles, 80 MB" would answer a question nobody asked.
  $("bundle-totals").textContent = `${index.count ?? 0} bundle${index.count === 1 ? "" : "s"} · ${formatBytes(index.bytes || 0)}${index.pinned ? ` · ${index.pinned} pinned` : ""}${index.links ? ` · ${index.links} reuse link${index.links === 1 ? "" : "s"}` : ""}`;
  renderPager({
    range: "bundle-range", prev: "bundle-prev", next: "bundle-next",
    offset: index.offset ?? 0, shown: shown.length,
    matched: index.matched ?? shown.length, noun: "bundle",
    filtered: Boolean(bundleQuery.q || bundleQuery.profile || bundleQuery.branch),
  });
  const branchChip = $("bundle-branch");
  branchChip.hidden = !bundleQuery.branch;
  branchChip.textContent = bundleQuery.branch ? `Branch: ${bundleQuery.branch === "-" ? "none" : bundleQuery.branch} ✕` : "";
  $("bundles").innerHTML = shown.map(entry => {
    const runs = bundleRuns(entry);
    const href = `#artifact/${escapeHtml(entry.id)}`;
    return `<tr class="clickable" data-href="${href}">
      <td class="bundle-name"><a class="row-link" href="${href}">${escapeHtml(entry.project || entry.profile || "unknown project")}</a>${bundleBadges(entry)}<small><code>${escapeHtml(entry.id.slice(0, 8))}</code> · ${bundleSource(entry)} · ${whenSpan(entry.created_at)}</small></td>
      <td>${bundleRevision(entry)}</td>
      <td>${bundleFamilies(entry)}</td>
      <td class="num">${escapeHtml(formatBytes(entry.bytes))}</td>
      <td class="nowrap">${runs === 1 ? "1 run" : `${runs} runs`}<small>${entry.last_used_at ? `last ${whenSpan(entry.last_used_at)}` : "never used"}</small></td>
      <td class="nowrap">${actorLink(entry.actor) || '<span class="muted">—</span>'}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="6" class="muted">${index.count ? "No bundle matches this filter." : `${Site()} keeps no firmware bundles.`}</td></tr>`;
}

function appSlot(family) {
  const app = family.app;
  if (!app?.size || !app?.slot) return "—";
  const share = app.size * 100 / app.slot;
  return `<span class="${share > 95 ? "bad" : ""}">${escapeHtml(share.toFixed(1))}%</span> <small class="muted">of ${escapeHtml(formatBytes(app.slot))}</small>`;
}

// A bundle is a page of its own: linkable, reloadable, and not something that
// appears below a table of forty others. Its lists -- the runs that used it,
// its files -- are paged here, because the current canary is flashed by every
// health check and would otherwise grow without end.
const BUNDLE_PAGE_SIZE = 10;
let bundlePage = {id: null, entry: null, runsOffset: 0, filesOffset: 0};

function pageControls(name, offset, total) {
  if (total <= BUNDLE_PAGE_SIZE) return "";
  const last = Math.min(offset + BUNDLE_PAGE_SIZE, total);
  return `<div class="pager"><span class="muted">${offset + 1}–${last} of ${total}</span><span class="pager-buttons"><button type="button" class="secondary page-step" data-list="${name}" data-step="-1"${offset <= 0 ? " disabled" : ""}>Previous</button><button type="button" class="secondary page-step" data-list="${name}" data-step="1"${last >= total ? " disabled" : ""}>Next</button></span></div>`;
}

function bundleActions(entry) {
  const locked = entry.pinned || entry.held;
  return `<button class="secondary bundle-download" data-id="${escapeHtml(entry.id)}" title="The bundle as .tar.gz: manifest beside the images, flashable as extracted">Download</button><button class="secondary bundle-pin admin-only" data-id="${escapeHtml(entry.id)}" data-pinned="${entry.pinned ? "1" : "0"}">${entry.pinned ? "Unpin" : "Pin"}</button><button class="danger bundle-delete admin-only" data-id="${escapeHtml(entry.id)}" data-bytes="${escapeHtml(entry.bytes)}"${locked ? ` disabled title="${entry.pinned ? "Pinned: unpin it to delete it" : "A queued or running job is using it"}"` : ""}>Delete</button>`;
}

async function showBundlePage(id, {keepOffsets = false} = {}) {
  // Eight characters is how every page shows a bundle, so it is what gets
  // copied into a message and pasted back. One bundle starting with them is
  // that bundle; the address is corrected to the whole id, so the link that
  // gets shared next is one that cannot become ambiguous.
  if (!/^[0-9a-f]{32}$/.test(id)) {
    bundlePage = {id, entry: null, runsOffset: 0, filesOffset: 0};
    $("bundle-title").textContent = "Bundle";
    $("bundle-subtitle").textContent = id;
    $("bundle-actions").innerHTML = "";
    ["bundle-images", "bundle-runs", "bundle-files"].forEach(section => { $(section).hidden = true; });
    const prefix = id.toLowerCase();
    let matches = [];
    if (/^[0-9a-f]{4,31}$/.test(prefix)) {
      $("bundle-overview").innerHTML = '<p class="muted">Finding the bundle…</p>';
      try {
        const found = await api(`/api/v1/artifacts?limit=25&q=${encodeURIComponent(prefix)}`);
        matches = (found.bundles || []).filter(bundle => bundle.id.startsWith(prefix));
      } catch (error) { matches = []; }
    }
    if (bundlePage.id !== id) return;
    if (matches.length === 1) {
      history.replaceState(null, "", `#artifact/${matches[0].id}`);
      lastRouted = location.hash;
      return showBundlePage(matches[0].id);
    }
    $("bundle-overview").innerHTML = matches.length
      ? `<p>${matches.length} bundles start with <code>${escapeHtml(id)}</code>:</p><ul>${matches.map(bundle => `<li><a href="#artifact/${escapeHtml(bundle.id)}"><code>${escapeHtml(bundle.id)}</code></a> ${escapeHtml(bundle.project || bundle.profile || "")}</li>`).join("")}</ul>`
      : `<p class="failure-summary">No bundle has the id <code>${escapeHtml(id)}</code>.</p><p class="muted">It may have been deleted or pruned. <a href="#artifacts">Back to artifacts</a></p>`;
    return;
  }
  if (!keepOffsets || bundlePage.id !== id) bundlePage = {id, entry: null, runsOffset: 0, filesOffset: 0};
  if (!bundlePage.entry) {
    $("bundle-title").textContent = "Bundle";
    $("bundle-subtitle").textContent = id.slice(0, 8);
    $("bundle-actions").innerHTML = "";
    $("bundle-overview").innerHTML = '<p class="muted">Loading the bundle…</p>';
    ["bundle-images", "bundle-runs", "bundle-files"].forEach(section => { $(section).hidden = true; });
  }
  let entry;
  try { entry = await api(`/api/v1/artifacts/${id}`); }
  catch (error) {
    if (bundlePage.id !== id) return;
    $("bundle-overview").innerHTML = `<p class="failure-summary">${escapeHtml(error.message)}</p><p class="muted">It may have been deleted or pruned. <a href="#artifacts">Back to artifacts</a></p>`;
    return;
  }
  // The operator may have moved on while this loaded.
  if (bundlePage.id !== id) return;
  bundlePage.entry = entry;
  renderBundlePage();
}

function renderBundlePage() {
  const entry = bundlePage.entry;
  if (!entry) return;
  const repo = entry.repo;
  $("bundle-title").textContent = entry.project || entry.profile || "Unknown project";
  $("bundle-subtitle").innerHTML = `Bundle <code>${escapeHtml(entry.id.slice(0, 8))}</code>${entry.revision ? ` · ${bundleRevision(entry)}` : ""}${bundleBadges(entry)}`;
  // Running a bundle is the farm's whole job; its page is where one is found.
  const runnable = entry.manifest_valid && entry.revision && profiles[entry.profile];
  const stale = entry.agent_current === false;
  $("bundle-actions").innerHTML = `${runnable ? `<button class="bundle-run"${stale ? ' disabled title="Built against an older HIL agent than this farm runs: a run would be refused"' : ' title="The run form, with this bundle chosen"'}>Run with this bundle</button>` : ""}${bundleActions(entry)}`;
  $("bundle-actions").querySelector(".bundle-run")?.addEventListener("click", () => runWithBundle(entry));
  const runs = bundleRuns(entry);
  $("bundle-overview").innerHTML = `<div class="live-facts">
      <div><small>Project</small><span>${escapeHtml(entry.project || "—")}${entry.profile ? ` <small class="muted">${escapeHtml(entry.profile)}</small>` : ""}</span></div>
      <div><small>Repository</small><span>${repoLink(repo) || "—"}</span></div>
      <div><small>Branch</small><span>${entry.branch ? `<span class="branch">${escapeHtml(entry.branch)}</span>` : '<span class="muted">not recorded</span>'}</span></div>
      <div><small>Revision</small><span>${entry.revision ? shaLink(entry.revision, repo) : "—"}</span></div>
      <div><small>Started by</small><span>${actorLink(entry.actor) || '<span class="muted">not recorded</span>'}</span></div>
      <div><small>Source</small><span>${bundleSource(entry, {long: true})}</span></div>
      <div><small>${entry.source?.kind === "supplied" ? "Received" : "Built"}</small><span>${escapeHtml(entry.created_at ? new Date(entry.created_at).toLocaleString() : "—")}</span></div>
      <div><small>Last used</small><span>${escapeHtml(entry.last_used_at ? new Date(entry.last_used_at).toLocaleString() : "never")}</span></div>
      <div><small>Size</small><span>${escapeHtml(formatBytes(entry.bytes))} · ${escapeHtml(entry.file_count)} files</span></div>
      <div><small>Flashed by</small><span>${runs === 1 ? "1 run" : `${runs} runs`}</span></div>
    </div>
    ${entry.pinned ? `<p class="muted">Pinned${entry.pinned_at ? ` ${escapeHtml(new Date(entry.pinned_at).toLocaleString())}` : ""}${entry.pin_note ? `: ${escapeHtml(entry.pin_note)}` : ""}. A pinned bundle is never pruned.</p>` : ""}`;

  const families = (entry.families || []).map(family => `<tr><td><strong>${escapeHtml(familyLabel(family.family))}</strong><small>${escapeHtml(family.family)}</small></td><td>${escapeHtml(family.board || "—")}</td><td>${escapeHtml(family.environment || "—")}</td><td class="num">${escapeHtml(formatBytes(family.image_bytes) || "—")}</td><td>${appSlot(family)}</td><td>${family.path && family.image_bytes != null ? `<button class="secondary bundle-file" data-path="${escapeHtml(family.path)}" title="${escapeHtml(family.image)}">Image</button>` : ""}</td></tr>`).join("");
  $("bundle-images").hidden = !families;
  $("bundle-images").innerHTML = `<div class="title-row"><div><p class="eyebrow">IMAGES</p><h2>One per family</h2></div><span class="muted">${escapeHtml((entry.families || []).length)} famil${(entry.families || []).length === 1 ? "y" : "ies"}</span></div><div class="table-wrap"><table class="compact"><thead><tr><th>Family</th><th>Board</th><th>Environment</th><th class="num">Image</th><th>App slot</th><th></th></tr></thead><tbody>${families}</tbody></table></div>`;

  // The run that built it (none, for a supplied bundle) and every run that
  // flashed it, newest first -- the same order as the run history.
  const users = [
    ...(entry.built_by ? [{...entry.built_by, created_at: entry.created_at, role: "built"}] : []),
    ...(entry.reused_by || []).map(run => ({...run, role: "flashed"})),
  ].sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  const runSlice = users.slice(bundlePage.runsOffset, bundlePage.runsOffset + BUNDLE_PAGE_SIZE);
  $("bundle-runs").hidden = false;
  $("bundle-runs").innerHTML = `<div class="title-row"><div><p class="eyebrow">RUNS</p><h2>Runs that used it</h2></div><span class="muted">${users.length ? `${users.length} run${users.length === 1 ? "" : "s"}` : ""}</span></div>
    ${users.length ? `<div class="table-wrap"><table class="compact"><thead><tr><th>Run</th><th>Status</th><th>Role</th><th>When</th></tr></thead><tbody>${runSlice.map(run => `<tr class="clickable" data-href="#run/${escapeHtml(run.id)}"><td><a class="row-link" href="#run/${escapeHtml(run.id)}"><code>${escapeHtml(run.id.slice(0, 8))}</code></a></td><td><span class="state ${statusClass(run.status)}">${escapeHtml(run.status)}</span></td><td class="muted">${escapeHtml(run.role)}</td><td class="nowrap">${whenSpan(run.created_at)}</td></tr>`).join("")}</tbody></table></div>${pageControls("runs", bundlePage.runsOffset, users.length)}` : '<p class="muted">No run in the history has used it.</p>'}`;

  const files = entry.files || [];
  const fileSlice = files.slice(bundlePage.filesOffset, bundlePage.filesOffset + BUNDLE_PAGE_SIZE);
  $("bundle-files").hidden = !files.length;
  $("bundle-files").innerHTML = `<div class="title-row"><div><p class="eyebrow">FILES</p><h2>What is in it</h2></div><span class="muted">${escapeHtml(files.length)} file${files.length === 1 ? "" : "s"}</span></div>
    <div class="table-wrap"><table class="compact"><thead><tr><th>Path</th><th class="num">Size</th><th>sha256</th><th></th></tr></thead><tbody>${fileSlice.map(file => `<tr><td><code>${escapeHtml(file.path)}</code></td><td class="num">${escapeHtml(formatBytes(file.bytes))}</td><td>${file.sha256 ? `<code title="${escapeHtml(file.sha256)}">${escapeHtml(file.sha256.slice(0, 12))}</code>` : '<span class="muted">not in the manifest</span>'}</td><td><button type="button" class="secondary bundle-file" data-path="${escapeHtml(file.path)}">${file.path.endsWith(".json") ? "View" : "Download"}</button></td></tr>`).join("")}</tbody></table></div>${pageControls("files", bundlePage.filesOffset, files.length)}`;

  const page = document.querySelector('.page[data-page="artifact"]');
  page.querySelectorAll(".bundle-file").forEach(button => button.addEventListener("click", () => {
    const path = `/api/v1/artifacts/${entry.id}/files/${button.dataset.path.split("/").map(encodeURIComponent).join("/")}`;
    (button.dataset.path.endsWith(".json") ? viewFromApi(path) : saveFromApi(path, button.dataset.path.replace("/", "-"))).catch(error => alert(`Could not open the file: ${error.message}`));
  }));
  page.querySelectorAll(".page-step").forEach(button => button.addEventListener("click", () => {
    const key = button.dataset.list === "runs" ? "runsOffset" : "filesOffset";
    bundlePage[key] = Math.max(0, bundlePage[key] + Number(button.dataset.step) * BUNDLE_PAGE_SIZE);
    renderBundlePage();
  }));
  installBundleHandlers($("bundle-actions"));
}

function installBundleHandlers(root) {
  root.querySelectorAll(".bundle-download").forEach(button => button.addEventListener("click", () => {
    button.disabled = true;
    saveFromApi(`/api/v1/artifacts/${button.dataset.id}/bundle`, `${button.dataset.id.slice(0, 8)}.tar.gz`)
      .catch(error => alert(`Download failed: ${error.message}`)).finally(() => { button.disabled = false; });
  }));
  root.querySelectorAll(".bundle-pin").forEach(button => button.addEventListener("click", async () => {
    const pinned = button.dataset.pinned === "1";
    let body = "{}";
    if (!pinned) {
      const note = prompt("Why keep this bundle? A pinned bundle is never pruned. (optional)", "");
      if (note === null) return;
      body = JSON.stringify(note.trim() ? {note: note.trim()} : {});
    }
    button.disabled = true;
    // A pin changes nothing on disk, so the storage figures are left alone.
    try {
      await api(`/api/v1/artifacts/${button.dataset.id}/${pinned ? "unpin" : "pin"}`, {method: "POST", body});
      await showBundlePage(button.dataset.id, {keepOffsets: true});
    } catch (error) { alert(`${pinned ? "Unpin" : "Pin"} failed: ${error.message}`); button.disabled = false; }
  }));
  root.querySelectorAll(".bundle-delete").forEach(button => button.addEventListener("click", async () => {
    const id = button.dataset.id;
    if (!confirm(`Delete bundle ${id.slice(0, 8)} (${formatBytes(Number(button.dataset.bytes))})?\n\nOnly the firmware images go. The runs that used it keep their reports, logs and serial captures, and say the images were pruned.`)) return;
    button.disabled = true;
    try {
      const removed = await api(`/api/v1/artifacts/${id}`, {method: "DELETE"});
      bundleChoices.loadedAt = 0;
      // The page it was on describes something that is gone.
      bundlePage = {id: null, entry: null, runsOffset: 0, filesOffset: 0};
      navigateTo("#artifacts");
      // The images are gone either way; what may have failed is the note
      // saying so, which is what the runs that used this bundle show
      // instead of pointing at nothing. A silent success would leave the
      // operator believing those pages will explain themselves.
      if (removed?.record_error) alert(`Bundle ${id.slice(0, 8)} was deleted, but recording the removal failed:\n\n${removed.record_error}\n\nThe runs that used it will not say when or why their images went.`);
    } catch (error) { alert(`Delete failed: ${error.message}`); button.disabled = false; }
  }));
}

// Measuring the disk runs on the farm in the background -- it can outlast a
// web request on the Pi -- so while it is going the panel asks again every
// few seconds, and stops when done or when the operator leaves the page.
let storagePollTimer = null;

function pollStorageSoon() {
  clearTimeout(storagePollTimer);
  storagePollTimer = setTimeout(() => {
    // Off the Artifacts page: opening it again loads it. Tab hidden: keep
    // waiting rather than stopping, so a measurement that finishes out of
    // sight is still picked up when the operator comes back.
    if ($("storage").closest(".page").hidden) return;
    if (document.hidden) return pollStorageSoon();
    loadStorage(false);
  }, 3000);
}

const known = value => value !== null && value !== undefined;

function storageTone(storage) {
  const pct = storage.filesystem?.used_percent;
  if (pct == null) return "";
  return storage.critical_percent && pct >= storage.critical_percent ? "bad" : storage.warn_percent && pct >= storage.warn_percent ? "warn" : "good";
}

// Each kind of storage in one row, and the row is the way to what fills it:
// a page listing its largest entries and the run each belongs to.
function renderStorage(storage) {
  lastStorage = storage;
  const fs = storage.filesystem || {};
  const pct = fs.used_percent;
  const tone = storageTone(storage);
  const thresholds = [storage.warn_percent ? `warns at ${storage.warn_percent}%` : "", storage.critical_percent ? `unhealthy at ${storage.critical_percent}%` : ""].filter(Boolean).join(", ");
  const rows = (storage.categories || []).map(category => {
    const size = known(category.bytes) ? formatBytes(category.bytes) : "measuring…";
    const detail = known(category.count) ? `${escapeHtml(category.count)} bundle${category.count === 1 ? "" : "s"}` : known(category.files) ? `${escapeHtml(category.files)} files` : "";
    // Bundles are listed on this page already; everything else has its own.
    const href = category.name === "artifacts" ? "#artifacts/all" : `#storage/${encodeURIComponent(category.name)}`;
    // Of what is used, not of the disk: the question is what to clear. A
    // share too small to round to a tenth still says it is there.
    const fraction = known(category.bytes) && fs.used ? category.bytes * 100 / fs.used : null;
    const share = fraction == null || category.bytes === 0 ? "" : fraction < 0.1 ? "<0.1%" : `${Math.round(fraction * 10) / 10}%`;
    return `<tr class="clickable" data-href="${escapeHtml(href)}"><td><a class="row-link" href="${escapeHtml(href)}">${escapeHtml(category.label)}</a><small>${escapeHtml(category.path || "")}</small></td><td class="num">${escapeHtml(size)}</td><td class="num muted">${escapeHtml(share)}</td><td class="muted">${detail}</td></tr>`;
  }).join("");
  const measured = storage.measuring
    ? "measuring the disk…"
    : `Measured ${escapeHtml(storage.measured_at ? new Date(storage.measured_at).toLocaleTimeString() : "just now")} · <button type="button" class="linkish storage-fresh">measure again</button>`;
  $("storage").innerHTML = `<div class="title-row"><div><p class="eyebrow">DISK</p><h2>Storage</h2></div><span class="muted">${measured}</span></div>
    ${pct != null ? `<p><strong class="${tone}">${escapeHtml(pct)}% used</strong> <span class="muted">${escapeHtml(formatBytes(fs.used))} of ${escapeHtml(formatBytes(fs.total))} · ${escapeHtml(formatBytes(fs.free))} free${thresholds ? ` · ${escapeHtml(thresholds)}` : ""}</span></p><div class="progress-track storage-track ${tone}"><span></span></div>` : ""}
    <div class="table-wrap"><table class="storage-table compact"><thead><tr><th>What</th><th class="num">Size</th><th class="num">Of used</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
  if (pct != null) setProgress($("storage"), pct / 100);
  $("storage").querySelector(".storage-fresh")?.addEventListener("click", () => loadStorage(true));
  clearTimeout(storagePollTimer);
  if (storage.measuring) pollStorageSoon();
}

async function loadStorage(fresh = false) {
  if (fresh) $("storage").querySelector(".storage-fresh")?.replaceWith(Object.assign(document.createElement("span"), {textContent: "measuring…"}));
  try { renderStorage(await api(`/api/v1/storage${fresh ? "?fresh=1" : ""}`)); }
  catch (error) { $("storage").innerHTML = `<p class="failure-summary">${escapeHtml(error.message)}</p>`; }
}

// ---- One kind of storage ------------------------------------------------
// What fills run evidence, the logs, the checkouts, the job database: largest first, a page at a time, each entry tied to the run it
// belongs to. All of it is from the farm's last measurement -- nothing here
// makes the Pi walk a directory while a request waits.
let lastStorage = null;
const storagePage = {kind: null, offset: 0, limit: 25};

async function loadStorageDetail(kind, offset = storagePage.kind === kind ? storagePage.offset : 0) {
  Object.assign(storagePage, {kind, offset});
  $("storage-title").textContent = "Storage";
  $("storage-subtitle").textContent = "";
  if (!$("storage-detail").dataset.kind || $("storage-detail").dataset.kind !== kind) {
    $("storage-detail").innerHTML = '<p class="muted">Loading…</p>';
  }
  $("storage-detail").dataset.kind = kind;
  let detail;
  try { detail = await api(`/api/v1/storage/${encodeURIComponent(kind)}?limit=${storagePage.limit}&offset=${offset}`); }
  catch (error) { $("storage-detail").innerHTML = `<p class="failure-summary">${escapeHtml(error.message)}</p>`; return; }
  if (storagePage.kind !== kind) return;
  renderStorageDetail(detail);
}

// A run's entry is its job id, the same 8 characters every other page shows;
// a log keeps its extension so it reads as a file.
function shortEntryName(name) {
  const match = /^([0-9a-f]{32})(\..+)?$/.exec(name);
  return match ? `${match[1].slice(0, 8)}${match[2] || ""}` : name;
}

function renderStorageDetail(detail) {
  $("storage-title").textContent = detail.label || "Storage";
  const size = known(detail.bytes) ? formatBytes(detail.bytes) : "not measured yet";
  const measuring = detail.measuring ? " · measuring…" : detail.measured_at ? ` · measured ${escapeHtml(new Date(detail.measured_at).toLocaleTimeString())}` : "";
  $("storage-subtitle").innerHTML = `<code>${escapeHtml(detail.path || "")}</code> · ${escapeHtml(size)}${known(detail.files) ? ` · ${escapeHtml(detail.files)} files` : ""}${measuring}`;

  if (detail.kind === "database") {
    const statuses = Object.entries(detail.by_status || {}).sort((a, b) => b[1] - a[1]).map(([status, count]) => `<span class="state ${statusClass(status)}">${escapeHtml(status)} ${escapeHtml(count)}</span>`).join(" ");
    $("storage-detail").innerHTML = `<div class="live-facts">
        <div><small>Size</small><span>${escapeHtml(size)}</span></div>
        <div><small>Jobs</small><span>${escapeHtml(detail.jobs ?? "—")}</span></div>
        <div><small>Oldest</small><span>${escapeHtml(detail.oldest ? new Date(detail.oldest).toLocaleString() : "—")}</span></div>
        <div><small>Newest</small><span>${escapeHtml(detail.newest ? new Date(detail.newest).toLocaleString() : "—")}</span></div>
        <div><small>Bundle records</small><span>${escapeHtml(detail.artifact_records ?? "—")}</span></div>
      </div>
      <p>${statuses || '<span class="muted">No jobs.</span>'}</p>
      <p class="muted">Every run, its request and its stages. Nothing prunes it yet; its size is the history, and the history is what the reports are made from.</p>`;
    return;
  }

  const entries = detail.entries || [];
  const rows = entries.map(entry => {
    const measured = known(entry.bytes);
    const job = entry.job;
    let what;
    if (job) {
      const href = `#run/${escapeHtml(job.id)}`;
      what = `<a class="row-link" href="${href}" title="${escapeHtml(entry.name)}"><code>${escapeHtml(shortEntryName(entry.name))}</code></a><small>${escapeHtml(job.project || profiles[job.profile]?.label || job.profile || job.kind)}${job.branch ? ` · ${escapeHtml(job.branch)}` : ""}</small>`;
    } else {
      what = `<code title="${escapeHtml(entry.name)}">${escapeHtml(shortEntryName(entry.name))}</code><small><span class="state warn" title="No run in the history owns this">orphan</span></small>`;
    }
    const status = job ? `<span class="state ${statusClass(job.status)}">${escapeHtml(job.status)}</span>` : "";
    const when = job?.created_at || entry.modified;
    return `<tr${job ? ` class="clickable" data-href="#run/${escapeHtml(job.id)}"` : ""}><td>${what}</td><td>${status}</td><td>${actorLink(job?.actor) || '<span class="muted">—</span>'}</td><td class="num">${escapeHtml(measured ? formatBytes(entry.bytes) : "measuring…")}</td><td class="num muted">${escapeHtml(entry.files ?? "")}</td><td class="nowrap">${whenSpan(when)}</td></tr>`;
  }).join("");
  const head = "<tr><th>Entry</th><th>Run</th><th>By</th><th class=\"num\">Size</th><th class=\"num\">Files</th><th>When</th></tr>";
  const empty = detail.matched === 0
    ? `<tr><td colspan="6" class="muted">${known(detail.bytes) ? "Nothing here." : "Not measured yet — the farm measures in the background; this page fills when it has."}</td></tr>`
    : "";
  const note = '<p class="muted">Largest first. Each entry is named for the run it belongs to; an orphan is one whose run has left the history.</p>';
  $("storage-detail").innerHTML = `<div class="table-wrap"><table class="compact"><thead>${head}</thead><tbody>${rows || empty}</tbody></table></div>
    <div class="pager"><span id="storage-range" class="muted"></span><span class="pager-buttons"><button id="storage-prev" class="secondary">Previous</button><button id="storage-next" class="secondary">Next</button></span></div>
    ${note}`;
  renderPager({range: "storage-range", prev: "storage-prev", next: "storage-next", offset: detail.offset ?? 0, shown: entries.length, matched: detail.matched ?? entries.length, noun: "entrie", filtered: false});
  $("storage-prev").addEventListener("click", () => loadStorageDetail(detail.kind, Math.max(0, (detail.offset ?? 0) - storagePage.limit)));
  $("storage-next").addEventListener("click", () => loadStorageDetail(detail.kind, (detail.offset ?? 0) + storagePage.limit));
  // A measurement under way: look again shortly.
  clearTimeout(storageDetailTimer);
  if (detail.measuring) {
    storageDetailTimer = setTimeout(() => {
      if (document.querySelector('.page[data-page="storage"]').hidden || storagePage.kind !== detail.kind) return;
      loadStorageDetail(detail.kind);
    }, 3000);
  }
}
let storageDetailTimer = null;

function bundleParams() {
  const params = new URLSearchParams({limit: bundleQuery.limit, offset: bundleQuery.offset});
  if (bundleQuery.q) params.set("q", bundleQuery.q);
  if (bundleQuery.profile) params.set("profile", bundleQuery.profile);
  if (bundleQuery.branch) params.set("branch", bundleQuery.branch);
  return params;
}

// One scan of the store a visit: each of these requests walks every bundle,
// and on a Pi two at once is two walks at once. The library on its tab, the
// list on the others; the figures above come from whichever was fetched.
async function loadArtifacts({storage = true} = {}) {
  if (firmwareTab === "library") await loadLibrary();
  else await loadBundleList();
  if (firmwareTab === "storage" && storage) {
    await loadStorage(false);
    loadRetention();
  }
}

async function loadBundleList() {
  try {
    artifactIndex = await api(`/api/v1/artifacts?${bundleParams()}`);
    renderBundles();
    renderFirmwareMetrics(artifactIndex);
  }
  catch (error) { $("bundles").innerHTML = `<tr><td colspan="6" class="failure-summary">${escapeHtml(error.message)}</td></tr>`; }
  // Someone's prune is deleting: follow it from here too, rather than leave
  // this page quiet until one of its requests collides with it. Not awaited
  // -- it runs for as long as the prune does.
  if (artifactIndex?.pruning?.active) followPrune();
}

function showFirmwareTab(tab, updateHash = true) {
  firmwareTab = ["library", "all", "storage"].includes(tab) ? tab : "library";
  $("firmware-library").hidden = firmwareTab !== "library";
  $("firmware-all").hidden = firmwareTab !== "all";
  $("firmware-storage").hidden = firmwareTab !== "storage";
  document.querySelectorAll("#firmware-tabs a").forEach(link => link.classList.toggle("active", link.dataset.tab === firmwareTab));
  if (updateHash) {
    history.replaceState(null, "", firmwareTab === "library" ? "#artifacts" : `#artifacts/${firmwareTab}`);
    lastRouted = location.hash;
  }
  if (token) loadArtifacts();
}

async function loadLibrary() {
  try { lastLibrary = await api("/api/v1/artifacts/library"); }
  catch (error) {
    $("firmware-library").innerHTML = `<section class="card"><p class="failure-summary">${escapeHtml(error.message)}</p></section>`;
    return;
  }
  renderFirmwareMetrics(lastLibrary);
  renderLibrary(lastLibrary);
  if (lastLibrary.pruning?.active) followPrune();
}

// The figures above the tabs: from the library when it was fetched, and from
// the list otherwise -- which counts the same store but not by branch, so
// what is older is the library's last word on it, or not said.
function renderFirmwareMetrics(summary) {
  if (shell().libraryMetrics) return shell().libraryMetrics(summary);
  const byBranch = Array.isArray(summary.projects);
  const projects = byBranch ? summary.projects.length : (summary.profiles || []).length;
  const branches = byBranch ? summary.projects.reduce((sum, project) => sum + project.branches.length, 0) : null;
  const older = byBranch ? summary : lastLibrary;
  const builtin = byBranch ? summary.projects.filter(project => project.builtin).length : 0;
  $("firmware-metrics").innerHTML = `
    <article><span>Projects</span><strong>${escapeHtml(projects)}</strong><small class="muted">${branches === null ? '<a href="#artifacts">in the library</a>' : `${escapeHtml(builtin)} built in · ${escapeHtml(branches)} branch${branches === 1 ? "" : "es"} built`}</small></article>
    <article><span>Builds kept</span><strong>${escapeHtml(summary.count ?? 0)}</strong><small class="muted">${summary.pinned ? `${escapeHtml(summary.pinned)} pinned` : "none pinned"}</small></article>
    <article><span>On disk</span><strong>${escapeHtml(formatBytes(summary.bytes || 0))}</strong><small class="muted">firmware bundles</small></article>
    <article><span>Older builds</span><strong>${older ? escapeHtml(formatBytes(older.older_bytes || 0)) : "–"}</strong><small class="muted">${!older ? '<a href="#artifacts">counted in the library</a>' : older.older ? `<a href="#artifacts/storage">${escapeHtml(older.older)} not the latest of their branch</a>` : "nothing older to clean up"}</small></article>`;
}

const LIBRARY_BRANCHES_SHOWN = 6;
function libraryRow(project, group, index) {
  const latest = group.latest;
  const href = `#artifact/${escapeHtml(latest.id)}`;
  const run = group.last_run;
  const runnable = group.runnable;
  const badges = [
    latest.pinned ? '<span class="state good">pinned</span>' : "",
    latest.held ? '<span class="state warn">in use</span>' : "",
    latest.imported_from ? `<span class="state muted">from ${escapeHtml(latest.imported_from)}</span>` : "",
    latest.agent_current === false && !latest.imported_from ? '<span class="state muted" title="Built against another HIL agent than this farm runs: a run would be refused">older agent</span>' : "",
  ].filter(Boolean).join(" ");
  const runButton = runnable
    ? `<button class="secondary library-run" data-id="${escapeHtml(runnable.id)}" title="${runnable.id === latest.id ? "The run form, with the latest build chosen" : `The newest build a run could flash: ${escapeHtml(String(runnable.revision || "").slice(0, 9))}`}">Run</button>`
    : "";
  return `<tr class="clickable${index >= LIBRARY_BRANCHES_SHOWN ? " library-more" : ""}" data-href="${href}"${index >= LIBRARY_BRANCHES_SHOWN ? " hidden" : ""}>
    <td>${group.branch ? `<span class="branch">${escapeHtml(group.branch)}</span>` : '<span class="muted">no branch</span>'}</td>
    <td><a class="row-link" href="${href}">${latest.revision ? escapeHtml(String(latest.revision).slice(0, 9)) : escapeHtml(latest.id.slice(0, 8))}</a> ${badges}<small>${whenSpan(latest.created_at)}${latest.actor ? ` · ${actorLink(latest.actor)}` : ""}</small></td>
    <td>${(latest.families || []).length ? `<div class="family-chips">${latest.families.map(family => `<span class="artifact">${escapeHtml(familyLabel(family))}</span>`).join("")}</div>` : '<span class="muted">none</span>'}</td>
    <td>${run ? `<a href="#run/${escapeHtml(run.id)}"><span class="state ${statusClass(run.status)}">${escapeHtml(run.status)}</span></a><small>${whenSpan(run.created_at)}</small>` : '<span class="muted">never run</span>'}</td>
    <td class="num">${group.bundles > 1 ? `<button type="button" class="linkish library-all" data-profile="${escapeHtml(project.profile)}" data-branch="${escapeHtml(group.key)}">${escapeHtml(group.bundles)} builds</button>` : "1 build"}${group.older ? `<small>${escapeHtml(group.older)} older · ${escapeHtml(formatBytes(group.older_bytes))}</small>` : ""}</td>
    <td class="num">${escapeHtml(formatBytes(group.bytes))}</td>
    <td class="actions">${runButton}</td>
  </tr>`;
}

// Each project a card -- what it is, its newest build, the last run on it,
// and on a portal whose it is and which rigs run it -- the built-ins first,
// with its builds by branch under it. A library is what can be run here,
// not what happens to be on disk.
function familyChips(families) {
  return `<div class="family-chips">${families.map(family => `<span class="artifact">${escapeHtml(familyLabel(family))}</span>`).join("")}</div>`;
}

function projectEyebrow(project) {
  if (project.builtin === "health-check") return "BUILT IN · HEALTH CHECK";
  if (project.builtin === "example") return "BUILT IN · EXAMPLE";
  const seen = project.visibility;
  if (!seen) return "PROJECT";
  if (seen.shown === "hidden") return "PROJECT · HIDDEN";
  return seen.repo === "public" ? "PROJECT · PUBLIC" : "PROJECT · PRIVATE";
}

function libraryCard(project) {
  const builtin = project.builtin;
  const own = Boolean(shell().projectsAreOwn);
  const blurb = builtin === "health-check"
    ? "Proves a board is wired, reachable and alive: the firmware a rig flashes before it trusts a board. Run from Boards, not as a project."
    : builtin === "example"
      ? "A small firmware and its suite, in a public repository to copy — and the project to try a rig with."
      : project.description || "";
  const families = project.families?.length ? familyChips(project.families)
    : builtin === "health-check" && project.firmware?.families?.length ? familyChips(project.firmware.families)
    : `<span class="muted">${project.exclusive ? "the whole bench" : "any boards"}</span>`;
  const branches = project.branches || [];
  const latest = project.latest;
  const runnable = branches.map(group => group.runnable).find(Boolean);
  const supply = project.supply_workflow;
  const newest = latest
    ? `<a href="#artifact/${escapeHtml(latest.id)}">${latest.revision ? escapeHtml(String(latest.revision).slice(0, 9)) : escapeHtml(latest.id.slice(0, 8))}</a>${branches[0]?.branch ? ` <span class="branch">${escapeHtml(branches[0].branch)}</span>` : ""}<small>${whenSpan(latest.created_at)}${latest.actor ? ` · ${actorLink(latest.actor)}` : ""}</small>`
    : builtin === "health-check" ? '<span class="muted">arrives with each release</span>'
    : `<span class="muted">none yet</span><small>${own && supply ? `<a href="#configuration/projects/${escapeHtml(project.profile)}">Get firmware from GitHub</a>` : supply ? `its CI hands one over (<code>${escapeHtml(supply)}</code>)` : "no supply workflow"}</small>`;
  const run = project.last_run;
  const verdict = run
    ? `<a href="#run/${escapeHtml(run.id)}"><span class="state ${statusClass(run.status)}">${escapeHtml(run.status)}</span></a><small>${whenSpan(run.created_at)}</small>`
    : '<span class="muted">never run</span>';
  const rigs = Array.isArray(project.rigs)
    ? `<div><small>Rigs</small><span>${project.rigs.length ? project.rigs.map(name => `<a href="${rigHref(name)}">${escapeHtml(name)}</a>`).join(", ") : '<span class="muted">none yet</span>'}</span></div>`
    : "";
  const firmware = builtin === "health-check" && project.firmware?.version
    ? `<div><small>Firmware</small><span>${escapeHtml(project.firmware.version)}</span></div>` : "";
  const kept = project.bundles
    ? `${escapeHtml(project.bundles)} build${project.bundles === 1 ? "" : "s"} · ${escapeHtml(formatBytes(project.bytes))}${project.pinned ? ` · ${escapeHtml(project.pinned)} pinned` : ""}`
    : '<span class="muted">nothing on disk</span>';
  const action = builtin === "health-check"
    ? `<button type="button" class="secondary library-go" data-href="${own ? "#boards" : "#rigs"}">${own ? "Boards" : "Rigs"}</button>`
    : runnable ? `<button type="button" class="secondary library-run" data-id="${escapeHtml(runnable.id)}">Run</button>` : "";
  const more = branches.length - LIBRARY_BRANCHES_SHOWN;
  return `<section class="card library-project${builtin ? " builtin" : ""}" data-profile="${escapeHtml(project.profile)}">
    <div class="title-row"><div><p class="eyebrow">${projectEyebrow(project)}</p><h2>${escapeHtml(project.label || project.project || project.profile)}</h2>${blurb ? `<small>${escapeHtml(blurb)}</small>` : ""}${project.repo ? `<small>${repoLink(project.repo)}</small>` : ""}</div><span class="actions">${action}</span></div>
    <div class="library-facts">
      <div><small>Newest build</small><span>${newest}</span></div>
      <div><small>Last run</small><span>${verdict}</span></div>
      <div><small>Boards</small><span>${families}</span></div>
      ${firmware}${rigs}
      <div><small>Kept</small><span>${kept}</span></div>
    </div>
    ${branches.length ? `<details class="library-builds"><summary>Builds by branch</summary>
      <div class="table-wrap"><table class="fleet library-table"><thead><tr><th>Branch</th><th>Latest build</th><th>Families</th><th>Last run</th><th class="num">Kept</th><th class="num">Size</th><th></th></tr></thead><tbody>${branches.map((group, index) => libraryRow(project, group, index)).join("")}</tbody></table></div>
      ${more > 0 ? `<button type="button" class="linkish library-show-more">Show ${escapeHtml(more)} more branch${more === 1 ? "" : "es"}</button>` : ""}
    </details>` : ""}
  </section>`;
}

function renderLibrary(library) {
  const projects = library.projects || [];
  const card = shell().libraryCard || libraryCard;
  $("firmware-library").innerHTML = projects.length ? projects.map(card).join("")
    : `<section class="card"><p class="muted">${escapeHtml(Site())} knows no project yet.</p></section>`;
}

function installLibraryHandlers() {
  const library = $("firmware-library");
  library.addEventListener("click", async event => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.classList.contains("library-show-more")) {
      button.closest(".library-project").querySelectorAll("tr.library-more").forEach(row => { row.hidden = false; });
      button.remove();
      return;
    }
    if (button.classList.contains("library-go")) return navigateTo(button.dataset.href);
    if (button.classList.contains("library-all")) {
      Object.assign(bundleQuery, {profile: button.dataset.profile, branch: button.dataset.branch, q: "", offset: 0});
      $("bundle-search").value = "";
      if ([...$("bundle-profile").options].some(option => option.value === button.dataset.profile)) $("bundle-profile").value = button.dataset.profile;
      return navigateTo("#artifacts/all");
    }
    if (button.classList.contains("library-run")) {
      button.disabled = true;
      try { runWithBundle(await api(`/api/v1/artifacts/${button.dataset.id}`)); }
      catch (error) { alert(`Could not open the build: ${error.message}`); }
      finally { button.disabled = false; }
    }
  });
}

// ---- Retention -----------------------------------------------------------------
// What the farm deletes on its own -- old serial and broker captures, old job
// logs, stale checkouts -- and a look at it before it does. A run's report
// and records are never part of it.
async function loadRetention() {
  try { renderRetention(await api("/api/v1/retention")); }
  catch (error) { $("retention").innerHTML = `<p class="failure-summary">${escapeHtml(error.message)}</p>`; }
}

function renderRetention(plan) {
  const settings = plan.settings || {};
  const runs = (plan.runs || []).length, logs = (plan.logs || []).length, checkouts = (plan.workspaces || []).length;
  const rule = `serial and broker captures of runs older than ${escapeHtml(settings.run_evidence_days)} days, job logs older than ${escapeHtml(settings.log_days)} days, checkouts left for ${escapeHtml(settings.workspace_days)} days — never the newest ${escapeHtml(settings.keep_newest_runs)} runs, never a queued or running one. Reports, JUnit and records stay.`;
  const due = runs + logs + checkouts
    ? `<p><strong>Due now:</strong> captures of ${runs} run${runs === 1 ? "" : "s"}, ${logs} log${logs === 1 ? "" : "s"}, ${checkouts} checkout${checkouts === 1 ? "" : "s"} — at most ${escapeHtml(formatBytes(plan.bytes_at_most || 0))}${plan.bytes_known ? "" : " (some not measured yet)"}.</p>`
    : '<p class="muted">Nothing is due under these rules.</p>';
  const last = plan.last
    ? `<p class="muted">Last sweep ${whenSpan(plan.last.finished_at)}: ${escapeHtml(plan.last.removed.runs)} runs' captures, ${escapeHtml(plan.last.removed.logs)} logs, ${escapeHtml(plan.last.removed.workspaces)} checkouts removed${(plan.last.errors || []).length ? `; ${escapeHtml(plan.last.errors.length)} could not be` : ""}.</p>`
    : "";
  $("retention").innerHTML = `<div class="title-row"><div><p class="eyebrow">RETENTION</p><h2>What ${site()} deletes on its own</h2></div><span class="state ${settings.enabled ? "good" : "warn"}">${settings.enabled ? "DAILY" : "OFF"}</span></div>
    <p class="muted">${settings.enabled ? "Every day: " : "Off: run evidence and logs are kept for good. Set <code>retention.enabled</code> in the host configuration to remove "}${rule}</p>
    ${due}${last}
    ${runs + logs + checkouts ? '<div class="actions admin-only"><button type="button" class="danger retention-run">Remove these now</button></div>' : ""}`;
  $("retention").querySelector(".retention-run")?.addEventListener("click", async event => {
    if (!confirm(`Remove the captures of ${runs} run(s), ${logs} log(s) and ${checkouts} checkout(s)?\n\nEach run keeps its report, JUnit and records, and its page says what went.`)) return;
    event.target.disabled = true;
    try { renderRetention(await api("/api/v1/retention/run", {method: "POST", body: JSON.stringify({dry_run: false})})); loadStorage(true); }
    catch (error) { alert(`Retention failed: ${error.message}`); event.target.disabled = false; }
  });
}

// A run's page links to the bundle it flashed.
function openBundle(id) {
  navigateTo(`#artifact/${id}`);
}

// A confirmed prune deletes on the farm and answers before it has finished:
// a thousand bundles of rmtree outlast the sixty seconds a request gets. The
// page follows the index's `pruning` until it is done, then says what went.
// Any load that finds one running follows it too -- after a reload, in a
// second tab, for another operator -- so a delete under way is never silent
// and its result reaches whoever is watching. One follower at a time.
let followingPrune = false;
async function sendPrune(rules, ids) {
  const result = await api("/api/v1/artifacts/prune", {method: "POST", body: JSON.stringify({...rules, ids, dry_run: false})});
  if (result.pruning) return followPrune(result);
  renderPruneResult(result);
  await loadArtifacts({storage: true});
  await refreshLibraryAfterPrune();
}

async function refreshLibraryAfterPrune() {
  lastLibrary = null;
  if (firmwareTab === "library") return loadLibrary();
  try {
    lastLibrary = await api("/api/v1/artifacts/library");
    renderLibrary(lastLibrary);
    renderFirmwareMetrics(lastLibrary);
  } catch { /* the figures say "–" until the library is opened */ }
}

function renderPruneProgress(active, selected) {
  // Before the first bundle goes there is nothing to count yet, only what
  // the prune set out to do.
  $("prune-preview").innerHTML = active
    ? `<p class="muted">Pruning\u2026 ${escapeHtml(active.removed)} of ${escapeHtml(active.total)}, ${escapeHtml(formatBytes(active.bytes))} freed.</p>`
    : `<p class="muted">Pruning ${escapeHtml(selected || 0)} bundle(s)\u2026</p>`;
}

async function followPrune(started) {
  if (followingPrune) return;
  followingPrune = true;
  try {
    renderPruneProgress(artifactIndex?.pruning?.active, (started?.selected || []).length);
    for (let attempt = 0; attempt < 900; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 2000));
      let index;
      try { index = await api(`/api/v1/artifacts?${bundleParams()}`); }
      catch (error) { continue; }  // a poll that fails is not a prune that failed
      artifactIndex = index;
      renderBundles();
      const progress = index.pruning || {};
      if (!progress.active) {
        renderPruneResult(progress.last || {});
        renderFirmwareMetrics(index);
        await loadStorage(false);
        // What the figures and the library said went with the prune: asked
        // again once it is done, not alongside it.
        await refreshLibraryAfterPrune();
        return;
      }
      renderPruneProgress(progress.active);
    }
  } finally { followingPrune = false; }
}

function renderPruneResult(result) {
  const removed = (result.removed || []).length;
  const spared = (result.spared || []).length;
  const tidied = (result.dangling_removed || []).length;
  const errors = result.errors || [];
  $("prune-preview").innerHTML = `<p class="good">Pruned ${escapeHtml(removed)} bundle${removed === 1 ? "" : "s"}, ${escapeHtml(formatBytes(result.bytes || 0))} freed.${spared ? ` <span class="muted">${escapeHtml(spared)} spared: in use, pinned, or no longer chosen since the preview.</span>` : ""}${tidied ? ` <span class="muted">${escapeHtml(tidied)} dangling reuse link${tidied === 1 ? "" : "s"} tidied.</span>` : ""}</p>${errors.length ? `<p class="failure-summary">${escapeHtml(errors.length)} could not be deleted: ${escapeHtml(errors.map(item => item.error).join("; "))}</p>` : ""}`;
}

function renderPrunePreview(preview, rules) {
  const bundles = preview.bundles || [];
  const dangling = (preview.dangling || []).length;
  const links = `${dangling} dangling reuse link${dangling === 1 ? "" : "s"}`;
  if (!bundles.length) {
    // Links left by runs whose bundle is gone hold nothing, but they are
    // only tidied by a confirmed prune: offer that, deleting no bundle.
    $("prune-preview").innerHTML = `<p class="muted">Nothing to prune under these rules.</p>${dangling ? `<div class="actions"><button type="button" class="secondary prune-tidy">Tidy ${escapeHtml(links)}</button><small class="muted">Left by runs whose bundle is gone; they hold nothing.</small></div>` : ""}`;
    const tidyButton = $("prune-preview").querySelector(".prune-tidy");
    tidyButton?.addEventListener("click", async () => {
      tidyButton.disabled = true;
      try {
        // No ids: not a single bundle may go, only links to nothing.
        await sendPrune(rules, []);
      } catch (error) { alert(`Tidy failed: ${error.message}`); tidyButton.disabled = false; }
    });
    return;
  }
  // A preview lists at most as many as a confirmation may send back, the
  // oldest first; past that it says how many more match.
  const more = Math.max(0, (preview.matched ?? bundles.length) - bundles.length);
  const beyond = more ? ` <span class="muted">${escapeHtml(more)} more match (${escapeHtml(formatBytes(preview.matched_bytes - preview.bytes))}); these are the oldest ${escapeHtml(bundles.length)}. Preview again after this prune for the rest.</span>` : "";
  const tidyNote = dangling ? ` <span class="muted">${escapeHtml(links)} will be tidied with it.</span>` : "";
  $("prune-preview").innerHTML = `<p><strong>${bundles.length} bundle${bundles.length === 1 ? "" : "s"}</strong>, ${escapeHtml(formatBytes(preview.bytes))} would be freed.${beyond}${tidyNote}</p>
    <div class="table-wrap"><table><thead><tr><th>Project</th><th>Bundle</th><th>Revision</th><th class="num">Size</th><th>Last used</th></tr></thead><tbody>${bundles.map(entry => `<tr><td>${escapeHtml(entry.project || entry.profile || "unknown")}</td><td><code>${escapeHtml(entry.id.slice(0, 8))}</code></td><td>${entry.revision ? `<code>${escapeHtml(String(entry.revision).slice(0, 10))}</code>` : "—"}</td><td class="num">${escapeHtml(formatBytes(entry.bytes))}</td><td>${escapeHtml(entry.last_used_at ? new Date(entry.last_used_at).toLocaleDateString() : "—")}</td></tr>`).join("")}</tbody></table></div>
    <div class="actions"><button type="button" class="danger prune-confirm">Delete these ${bundles.length} bundle${bundles.length === 1 ? "" : "s"}</button><small class="muted">The runs that used them keep their reports, logs and serial captures.</small></div>`;
  const confirmButton = $("prune-preview").querySelector(".prune-confirm");
  confirmButton.addEventListener("click", async () => {
    if (!confirm(`Delete ${bundles.length} bundle(s), ${formatBytes(preview.bytes)}? This cannot be undone.`)) return;
    confirmButton.disabled = true;
    try {
      // What was shown is what may go: the farm deletes only these, and of
      // these only what the rule still chooses -- a run that started since
      // may hold one, and a bundle that became eligible since is not deleted
      // unseen.
      await sendPrune(rules, bundles.map(entry => entry.id));
    } catch (error) { alert(`Prune failed: ${error.message}`); confirmButton.disabled = false; }
  });
}

$("prune-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = new FormData(event.target);
  const rules = {};
  for (const name of ["older_than_days", "keep_per_profile"]) {
    const value = String(form.get(name) ?? "").trim();
    if (value !== "") rules[name] = Number(value);
  }
  if (!Object.keys(rules).length) return alert("Give at least one rule: an age, a number to keep per project, or both.");
  try { renderPrunePreview(await api("/api/v1/artifacts/prune", {method: "POST", body: JSON.stringify({...rules, dry_run: true})}), rules); }
  catch (error) { alert(`${Site()} refused the prune: ${error.message}`); }
});
$("artifacts-refresh").addEventListener("click", () => loadArtifacts());
$("close-bundle").textContent = "Back to firmware";
// A row is a link: clicking anywhere on it opens what it describes, except on
// something inside it that is a link or a control of its own.
document.addEventListener("click", event => {
  const row = event.target.closest("tr.clickable[data-href]");
  if (!row || event.target.closest("a, button, input, select, label")) return;
  navigateTo(row.dataset.href);
});
$("stats-refresh").addEventListener("click", () => loadStatistics(statsDays));
$("close-bundle").addEventListener("click", () => navigateTo("#artifacts"));
$("close-storage").addEventListener("click", () => navigateTo("#artifacts"));
// A changed filter starts at its first page: staying on page four of a
// search that now matches two things shows an empty table and looks broken.
let bundleSearchDebounce = null;
$("bundle-search").addEventListener("input", event => {
  bundleQuery.q = event.target.value;
  bundleQuery.offset = 0;
  clearTimeout(bundleSearchDebounce);
  bundleSearchDebounce = setTimeout(() => loadBundleList(), 250);
});
$("bundle-prev").addEventListener("click", () => {
  bundleQuery.offset = Math.max(0, bundleQuery.offset - bundleQuery.limit);
  loadBundleList();
});
$("bundle-next").addEventListener("click", () => {
  bundleQuery.offset += bundleQuery.limit;
  loadBundleList();
});
$("bundle-branch").addEventListener("click", () => {
  bundleQuery.branch = "";
  bundleQuery.offset = 0;
  loadBundleList();
});
installLibraryHandlers();
$("bundle-profile").addEventListener("change", event => {
  bundleQuery.profile = event.target.value;
  bundleQuery.branch = "";
  bundleQuery.offset = 0;
  loadBundleList();
});

// ---- Statistics -------------------------------------------------------------
// What the farm has done, from its own job history. The service computes it
// (`/api/v1/stats`) in the viewer's time zone, so "yesterday" is the
// operator's. The charts are SVG built from attributes and classes -- the
// service's content security policy allows no inline style, and a chart
// library would be the only third-party code on the page.
const STATS_WINDOWS = [7, 30, 90];
let statsDays = 7;
let lastStats = null;
let statsTimer = null;
let overviewStatsAt = 0;

function tzOffsetMinutes() { return -new Date().getTimezoneOffset(); }

function percent(fraction, digits = 0) {
  return fraction === null || fraction === undefined ? "—" : `${(fraction * 100).toFixed(digits)}%`;
}

function shortDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

async function fetchStats(days) {
  return api(`/api/v1/stats?days=${days}&tz_offset_minutes=${tzOffsetMinutes()}`);
}

// Stacked bars for passed, failed and cancelled; beneath them, how many hours
// the rig held its lock that day. Days with nothing are drawn as nothing,
// which is itself worth seeing.
function runsChart(stats) {
  const days = stats.per_day || [];
  if (!days.length) return '<p class="muted">No days in this window.</p>';
  const width = 1000, top = 150, gap = 12, busyHeight = 34;
  const slot = width / days.length;
  const bar = Math.max(2, slot - Math.min(6, slot * 0.25));
  const peak = Math.max(1, ...days.map(day => day.passed + day.failed + day.cancelled));
  const label = date => new Date(`${date}T12:00:00`).toLocaleDateString([], {month: "short", day: "numeric"});
  // Enough labels to read, never so many they collide.
  const every = Math.max(1, Math.ceil(days.length / 10));
  const rects = days.map((day, index) => {
    const x = (index * slot + (slot - bar) / 2).toFixed(1);
    let y = top;
    const parts = [["passed", day.passed], ["failed", day.failed], ["cancelled", day.cancelled]].map(([kind, count]) => {
      if (!count) return "";
      const height = count / peak * (top - 8);
      y -= height;
      return `<rect class="bar-${kind}" x="${x}" y="${y.toFixed(1)}" width="${bar.toFixed(1)}" height="${height.toFixed(1)}"><title>${escapeHtml(label(day.date))}: ${count} ${kind}</title></rect>`;
    }).join("");
    const hours = day.busy_seconds / 3600;
    const busy = hours > 0 ? `<rect class="bar-busy" x="${x}" y="${(top + gap + busyHeight - Math.min(1, hours / 24) * busyHeight).toFixed(1)}" width="${bar.toFixed(1)}" height="${(Math.min(1, hours / 24) * busyHeight).toFixed(1)}"><title>${escapeHtml(label(day.date))}: rig busy ${hours.toFixed(1)} h</title></rect>` : "";
    const text = index % every === 0 || index === days.length - 1 ? `<text class="chart-label" x="${(index * slot + slot / 2).toFixed(1)}" y="${top + gap + busyHeight + 18}" text-anchor="middle">${escapeHtml(label(day.date))}</text>` : "";
    return parts + busy + text;
  }).join("");
  const height = top + gap + busyHeight + 26;
  return `<svg class="runs-chart" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="Suite runs per day and rig busy hours">
    <line class="chart-axis" x1="0" y1="${top}" x2="${width}" y2="${top}"></line>
    <text class="chart-label" x="2" y="12">busiest day: ${peak} run${peak === 1 ? "" : "s"}</text>
    <text class="chart-label" x="2" y="${top + gap + 10}">rig busy (of 24 h)</text>
    ${rects}
  </svg>`;
}

// Horizontal bars for a breakdown: label, count, share.
function breakdown(items, {empty}) {
  const total = items.reduce((sum, item) => sum + item.count, 0);
  if (!total) return `<p class="muted">${empty}</p>`;
  const peak = Math.max(...items.map(item => item.count));
  return `<div class="hbars">${items.map(item => {
    const share = item.count / total;
    return `<div class="hbar-row"><span class="hbar-label">${escapeHtml(item.label)}</span><svg class="hbar" viewBox="0 0 100 10" preserveAspectRatio="none" aria-hidden="true"><rect class="hbar-track" x="0" y="0" width="100" height="10"></rect><rect class="${item.tone || "hbar-fill"}" x="0" y="0" width="${(item.count / peak * 100).toFixed(1)}" height="10"></rect></svg><span class="hbar-count">${escapeHtml(item.count)} <small>${escapeHtml(percent(share))}</small></span></div>`;
  }).join("")}</div>`;
}

const FIRMWARE_SOURCES = [
  ["supplied", "Supplied by CI", "hbar-good"],
  ["reused", "Held bundle, same commit", "hbar-good"],
  // The farm no longer builds; these are what a long window still reaches.
  ["built", "Built on the rig (retired)", "hbar-fill"],
  ["build_failed", "Rig build failed (retired)", "hbar-bad"],
  ["not_reached", "Ended before firmware", "hbar-muted"],
];

function renderStatistics(stats) {
  lastStats = stats;
  const totals = stats.totals || {};
  const boards = stats.boards || {};
  const span = stats.window || {};
  $("stats-window").innerHTML = STATS_WINDOWS.map(days => `<button type="button" class="chip${days === span.days ? " active" : ""}" data-days="${days}">${days} days</button>`).join("");
  $("stats-window").querySelectorAll(".chip").forEach(chip => chip.addEventListener("click", () => {
    history.replaceState(null, "", `#statistics/${chip.dataset.days}`);
    lastRouted = location.hash;
    loadStatistics(Number(chip.dataset.days));
  }));
  const tile = (label, value, detail = "", tone = "") => `<article><span>${label}</span><strong class="${tone}">${value}</strong>${detail ? `<small>${detail}</small>` : ""}</article>`;
  const rateTone = totals.pass_rate == null ? "" : totals.pass_rate >= 0.9 ? "good" : totals.pass_rate >= 0.6 ? "warn" : "bad";
  const healthy = boards.connected ? `${boards.passed}/${boards.connected}` : "—";
  $("stats-headline").innerHTML = [
    tile("Suite runs", escapeHtml(totals.runs ?? 0), `${escapeHtml(totals.passed ?? 0)} passed · ${escapeHtml(totals.failed ?? 0)} failed${totals.cancelled ? ` · ${escapeHtml(totals.cancelled)} cancelled` : ""}`),
    tile("Pass rate", escapeHtml(percent(totals.pass_rate)), "passed of passed + failed", rateTone),
    tile("Run time", escapeHtml(shortDuration(stats.duration?.median)), `median · slowest tenth ${escapeHtml(shortDuration(stats.duration?.p90))}`),
    tile("Queue wait", escapeHtml(shortDuration(stats.queue_wait?.median)), `median · worst ${escapeHtml(shortDuration(stats.queue_wait?.max))}`),
    tile("Rig busy", escapeHtml(percent(stats.utilisation?.fraction, 1)), `${escapeHtml(((stats.utilisation?.busy_seconds || 0) / 3600).toFixed(1))} h of ${escapeHtml(span.days)} days`),
    tile("Boards healthy", escapeHtml(healthy), boards.last_checked ? `checked ${escapeHtml(relativeWhen(boards.last_checked))}` : "no health check yet", boards.failed ? "bad" : boards.connected && boards.passed === boards.connected ? "good" : ""),
  ].join("");

  $("stats-legend").innerHTML = '<span class="legend legend-passed">passed</span> <span class="legend legend-failed">failed</span> <span class="legend legend-cancelled">cancelled</span> <span class="legend legend-busy">rig busy</span>';
  $("stats-chart").innerHTML = runsChart(stats);

  const projects = stats.by_project || [];
  $("stats-projects").innerHTML = projects.length
    ? `<div class="table-wrap"><table class="compact"><thead><tr><th>Project</th><th class="num">Runs</th><th class="num">Passed</th><th class="num">Failed</th><th class="num">Cancelled</th><th>Pass rate</th><th class="num">Median run</th><th class="num">Slowest tenth</th><th>Last run</th></tr></thead><tbody>${projects.map(project => {
      const rate = project.pass_rate;
      const tone = rate == null ? "hbar-muted" : rate >= 0.9 ? "hbar-good" : rate >= 0.6 ? "hbar-fill" : "hbar-bad";
      return `<tr><td><strong>${escapeHtml(project.project)}</strong><small class="muted"> ${escapeHtml(project.profile)}</small></td><td class="num">${escapeHtml(project.runs)}</td><td class="num">${escapeHtml(project.passed)}</td><td class="num">${escapeHtml(project.failed)}</td><td class="num muted">${escapeHtml(project.cancelled)}</td><td class="rate-cell"><svg class="hbar" viewBox="0 0 100 10" preserveAspectRatio="none" aria-hidden="true"><rect class="hbar-track" x="0" y="0" width="100" height="10"></rect><rect class="${tone}" x="0" y="0" width="${rate == null ? 0 : (rate * 100).toFixed(1)}" height="10"></rect></svg> ${escapeHtml(percent(rate))}</td><td class="num">${escapeHtml(shortDuration(project.median_duration))}</td><td class="num muted">${escapeHtml(shortDuration(project.p90_duration))}</td><td class="nowrap"><span class="state ${statusClass(project.last_status)}">${escapeHtml(project.last_status || "—")}</span> ${whenSpan(project.last_run_at)}</td></tr>`;
    }).join("")}</tbody></table></div>`
    : '<p class="muted">No suite runs in this window.</p>';

  $("stats-stages").innerHTML = breakdown(
    (stats.failed_stages || []).map(stage => ({label: stage.label === "unrecorded" ? "Not recorded" : stage.label, count: stage.count, tone: "hbar-bad"})),
    {empty: "No failed runs in this window."},
  );
  const firmware = stats.firmware || {};
  $("stats-firmware").innerHTML = breakdown(
    FIRMWARE_SOURCES.filter(([key]) => firmware[key]).map(([key, label, tone]) => ({label, count: firmware[key], tone})),
    {empty: "No suite runs in this window."},
  );
  $("stats-note").innerHTML = `From ${escapeHtml(new Date(span.from).toLocaleDateString())} to now, in your time zone. Runs are suite runs; ${escapeHtml(stats.discoveries ?? 0)} hardware discover${stats.discoveries === 1 ? "y" : "ies"} ran as well. Rig busy is the time a job held the rig's lock. Nothing here is collected for this page: it is the farm's own job history.`;
}

async function loadStatistics(days = statsDays) {
  statsDays = STATS_WINDOWS.includes(days) ? days : 7;
  clearTimeout(statsTimer);
  if (!lastStats) $("stats-chart").innerHTML = '<p class="muted">Loading…</p>';
  try { renderStatistics(await fetchStats(statsDays)); }
  catch (error) { $("stats-chart").innerHTML = `<p class="failure-summary">${escapeHtml(error.message)}</p>`; }
  // History moves slowly; once a minute while the page is open is plenty.
  statsTimer = setTimeout(() => {
    if (document.querySelector('.page[data-page="statistics"]').hidden || document.hidden) return;
    loadStatistics(statsDays);
  }, 60000);
}

// The overview's summary of the last week: the questions an owner asks first,
// and a way to the rest.
async function loadOverviewStats() {
  // `/api/v1/stats` is a farm-wide read and is closed to an account, so for
  // one this was a 403 on every poll: the band it fills is hidden for them
  // anyway, and the catch below swallowed the refusal quietly enough that
  // the only sign was a failing request once a minute, forever.
  if (workspaceOnly()) return;
  if (Date.now() - overviewStatsAt < 60000) return;
  overviewStatsAt = Date.now();
  let stats;
  try { stats = await fetchStats(7); }
  catch (error) { overviewStatsAt = 0; return; }
  const totals = stats.totals || {};
  const boards = stats.boards || {};
  const days = (stats.per_day || []);
  const peak = Math.max(1, ...days.map(day => day.passed + day.failed + day.cancelled));
  const spark = days.map((day, index) => {
    const x = index * 14 + 1;
    let y = 30;
    return [["passed", day.passed], ["failed", day.failed], ["cancelled", day.cancelled]].map(([kind, count]) => {
      if (!count) return "";
      const height = count / peak * 28;
      y -= height;
      return `<rect class="bar-${kind}" x="${x}" y="${y.toFixed(1)}" width="10" height="${height.toFixed(1)}"></rect>`;
    }).join("");
  }).join("");
  const rateTone = totals.pass_rate == null ? "" : totals.pass_rate >= 0.9 ? "good" : totals.pass_rate >= 0.6 ? "warn" : "bad";
  const item = (label, value, tone = "") => `<div><small>${label}</small><strong class="${tone}">${value}</strong></div>`;
  $("overview-stats").hidden = false;
  $("overview-stats").innerHTML = `<div class="title-row"><div><p class="eyebrow">LAST 7 DAYS</p><h2>How ${site()} is doing</h2></div><button type="button" class="secondary go-statistics">Statistics</button></div>
    <div class="stats-band-row">
      <svg class="spark" viewBox="0 0 ${days.length * 14} 30" preserveAspectRatio="none" role="img" aria-label="Suite runs per day, last 7 days">${spark}</svg>
      ${item("Suite runs", escapeHtml(totals.runs ?? 0))}
      ${item("Pass rate", escapeHtml(percent(totals.pass_rate)), rateTone)}
      ${item("Median run", escapeHtml(shortDuration(stats.duration?.median)))}
      ${item("Worst queue wait", escapeHtml(shortDuration(stats.queue_wait?.max)))}
      ${item("Rig busy", escapeHtml(percent(stats.utilisation?.fraction, 1)))}
      ${item("Boards healthy", escapeHtml(boards.connected ? `${boards.passed}/${boards.connected}` : "—"), boards.failed ? "bad" : boards.connected && boards.passed === boards.connected ? "good" : "")}
    </div>`;
  $("overview-stats").querySelector(".go-statistics").addEventListener("click", () => navigateTo("#statistics"));
}

// ---- Configuration ---------------------------------------------------------
// What the farm is configured to do, read from the running service rather than
// restated here, so a setting changed on the host shows up instead of the
// page's idea of it.
// Every parameter the host's configuration file can carry
// (runner/hil-config.schema.json), so this page answers what the farm is set
// to without an ssh session. Read-only, and one thing is deliberately absent:
// the gateway password. Its *path* is here, because an operator needs to know
// which file to rotate; the secret itself has no business in a web page.
// A portal has no boards, no rig, no runner and no host file of the Pi's
// shape: the sections marked `host` describe a farm host, and on a portal they
// would show defaults as though they were settings. Its nodes' are shown
// under each node instead.
const HOST_ONLY = new Set(["host", "service", "paths", "health", "gateway", "mqtt", "farm", "callmebot"]);
const CONFIG_SECTIONS = [
  ["Host", "host", {hostname: "Hostname", mode: "Mode", runner_unit: "Runner unit", state_free_gb: "Free disk (GB)", config_schema: "Config schema"}],
  ["Service", "service", {enabled: "Enabled", bind: "Bind address", port: "Port", public_host: "Public host", token_file: "API token file", suite_timeout_seconds: "Suite timeout (s)", rig_lock: "Rig lock", concurrency: "Runs at once"}],
  ["Paths", "paths", {repo: "Repository", state: "State", inventory: "Inventory registry", board_map: "Active board map", venv: "Virtualenv", python: "Interpreter"}],
  ["Health checks", "health", {interval_minutes: "Interval (min)", minimum_boards: "Minimum boards", disk_warn_percent: "Disk warn (%)", disk_critical_percent: "Disk critical (%)"}],
  // The rig's own network. Not decoration: the canary's radio, uplink and
  // queue checks skip when these are unset, and "why did half the health
  // checks skip" should be answerable from here.
  ["Rig network", "gateway", {enabled: "Access point", ssid: "SSID", password_file: "Password file", endpoint: "Uplink probe", channel: "AP channel"}],
  ["Rig broker", "mqtt", {enabled: "Broker", url: "URL"}],
  // Whether a board plugged in registers itself, which is what makes a rig
  // something you wire up rather than something you enrol.
  ["Boards", "inventory", {auto_register: "Register new boards automatically"}],
  // A real service the rig validates with its owner's own link
  // (docs/providers.md): the file's path and when a message may be sent,
  // never the link.
  ["CallMeBot provider", "callmebot", {url_file: "Link file", send: "Sends real messages", max_per_day: "Messages per day", used_today: "Used today (UTC)"}],
  // Where the farm says it broke, and how the last message went: a webhook
  // nobody tested is found to be wrong by the failure it was meant to report.
  // Where the farm says it broke. One row per setting a channel uses: the
  // webhook's URL file and format, Telegram's token file and chat.
  ["Notifications", "notify", {enabled: "Notifications", channel: "Channel", webhook_url_file: "Webhook URL file", format: "Format", token_file: "Bot token file", chat_id: "Chat", events: "Events", last_delivery: "Last delivery"}],
  ["Backup", "backup", {enabled: "Nightly backup", directory: "Directory", keep: "Kept", target: "Copied to", last_backup: "Last backup"}],
  // Standalone, a portal, or a node taking its runs from one (docs/portal-plan.md).
  ["Farm role", "farm", {mode: "Role", portal_url: "Portal", worker_name: "Worker name", node_key_file: "Node key file"}],
  // A board the canary keeps failing is left out of runs until a clean check.
  ["Quarantine", "quarantine", {enabled: "Automatic quarantine", after_failures: "After failed health checks in a row"}],
  ["Retention", "retention", {enabled: "Retention", run_evidence_days: "Captures kept (days)", log_days: "Logs kept (days)", keep_newest_runs: "Newest runs always kept", workspace_days: "Stale checkouts (days)"}],
];

function settingValue(value) {
  // "on"/"off" rather than true/false: a table of settings reads as prose,
  // and a false that means "this feature is off" is worth showing rather
  // than dropping as though the setting did not exist.
  if (value === true) return "on";
  if (value === false) return "off";
  return String(value);
}

function renderConfigSections(config, {portal = false, skip = []} = {}) {
  return CONFIG_SECTIONS.filter(([, key]) => !(portal && HOST_ONLY.has(key)) && !skip.includes(key)).map(([title, key, labels]) => {
    const rows = Object.entries(labels)
      .filter(([field]) => config?.[key]?.[field] !== undefined && config[key][field] !== null)
      .map(([field, label]) => `<div><small>${escapeHtml(label)}</small><span>${escapeHtml(settingValue(config[key][field]))}</span></div>`);
    return rows.length ? `<h3>${escapeHtml(title)}</h3><div class="detail-grid">${rows.join("")}</div>` : "";
  }).join("");
}

// A group of settings as they read: each with its value and what it does.
function viewGroup(title, note, rows) {
  const shown = rows.filter(Boolean);
  if (!shown.length) return "";
  return `<section class="setting-group"><h3>${escapeHtml(title)}</h3>${note ? `<p class="muted">${escapeHtml(note)}</p>` : ""}${shown.map(([label, value, help]) => `<div class="setting"><div><strong>${escapeHtml(label)}</strong>${help ? `<small class="muted">${escapeHtml(help)}</small>` : ""}</div><span class="value">${value}</span></div>`).join("")}</section>`;
}
const onOff = value => value === true ? '<span class="good">on</span>' : value === false ? '<span class="muted">off</span>' : '<span class="muted">not set</span>';
const plainValue = (value, unit = "") => value === null || value === undefined || value === ""
  ? '<span class="muted">not set</span>'
  : `${escapeHtml(Array.isArray(value) ? value.join(", ") : value)}${unit ? ` ${escapeHtml(unit)}` : ""}`;

// The farm's own notifications. A rig tells its owner about itself; this is
// the other audience -- whoever looks after the farm -- told the things a rig
// cannot say, least of all one that has gone quiet.
let farmNotify = null;

function renderFarmNotify() {
  const card = $("farm-notify");
  if (!shell().releases) { card.hidden = true; return; }
  card.hidden = false;
  const channels = farmNotify?.channels || [];
  const editing = farmNotifyEditing;   // false, "new", or the id being changed
  const heading = `<div class="title-row"><div><p class="eyebrow">THE FARM'S OWN</p><h2>Notifications</h2></div>`
    + `<div class="row-actions">${channels.length
        ? `<span class="muted">${escapeHtml(String(channels.length))} channel${channels.length > 1 ? "s" : ""}</span>`
        : '<span class="state muted">not set</span>'}${
        isAdmin() && !editing ? `<button class="secondary farm-notify-open" data-id="">Add channel</button>` : ""}</div></div>`;
  if (editing) {
    // Adding one, or changing the one whose id this is: a farm may send
    // through a bot and a webhook at once, so the form belongs to a channel
    // rather than to the farm.
    const changing = channels.find(item => item.id === editing) || {};
    const kind = farmNotifyKind || changing.channel || "telegram";
    const spec = CHANNEL_KINDS[kind];
    const extra = kind === "telegram"
      ? `<label class="setting"><div><strong>Chat</strong><small class="muted">The chat to send to: 12345678, or -1001234567890 for a group.</small></div><span class="field"><input name="chat_id" required pattern="-?[0-9]{1,20}" value="${escapeHtml(changing.chat_id || "")}"></span></label>`
      : kind === "webhook"
      ? `<label class="setting"><div><strong>Shape</strong><small class="muted">How the body is written.</small></div><span class="field"><select name="format">${["slack", "discord", "json"].map(option => `<option value="${option}"${changing.format === option ? " selected" : ""}>${option}</option>`).join("")}</select></span></label>`
      : "";
    return renderSection(card, `${heading}<form id="farm-notify-form" autocomplete="off" data-id="${escapeHtml(editing === "new" ? "" : editing)}">
      <label class="setting"><div><strong>Channel</strong><small class="muted">${escapeHtml(spec.how)}</small></div>
        <span class="field"><select name="kind" class="farm-notify-kind">${Object.entries(CHANNEL_KINDS).map(([value, item]) =>
          `<option value="${escapeHtml(value)}"${value === kind ? " selected" : ""}>${escapeHtml(item.title)}</option>`).join("")}</select></span></label>
      <label class="setting"><div><strong>${escapeHtml(spec.secret)}</strong><small class="muted">Kept on the portal, readable by nothing else, and never shown again.</small></div>
        <span class="field"><input type="password" name="credential" autocomplete="off" spellcheck="false" required></span></label>
      ${extra}
      <div class="settings-footer"><button type="submit">Save</button><button type="button" class="secondary farm-notify-cancel">Cancel</button></div></form>`);
  }
  if (!channels.length) {
    return renderSection(card, `${heading}<p class="muted">Nobody is told when a rig goes quiet, falls behind or joins.`
      + `${isAdmin() ? " Add a channel above — a Telegram bot, or the webhook your own ingestion listens on." : ""}</p>`);
  }
  // One card per channel, each with its own buttons: they are separate
  // things, and a test that says "delivered" should say which one delivered.
  const cards = channels.map(item => {
    const last = item.last_delivery;
    const state = !item.enabled ? {tone: "muted", label: "off"}
      : last && last.ok === false ? {tone: "bad", label: "not working"}
      : {tone: "good", label: "on"};
    const where = item.channel === "telegram" ? `chat ${item.chat_id || "?"}`
      : item.channel === "callmebot" ? "the stored link"
      : `${item.format || "slack"} webhook`;
    return `<div class="channel-card"><div><strong>${escapeHtml(CHANNEL_KINDS[item.channel]?.title || item.channel)}</strong>`
      + `<span class="state ${state.tone}">${escapeHtml(state.label)}</span></div>`
      + `<small>${escapeHtml(where)} · credential in <code>${escapeHtml(item.secret_file || "")}</code></small>`
      + `<small>sends: ${escapeHtml((item.events || []).join(", "))}</small>`
      + `<small>${last ? `last message ${escapeHtml(shortWhen(last.at) || "")}: ${last.ok ? "delivered" : escapeHtml(shortDetail(last.error || "failed", 120))}`
        : "no message sent down it yet"}</small>`
      + (item.updated_at ? `<small>set ${escapeHtml(shortWhen(item.updated_at) || "")}${item.updated_by ? ` by ${escapeHtml(item.updated_by)}` : ""}</small>` : "")
      + (isAdmin() ? `<div class="row-actions"><button class="secondary farm-notify-test" data-id="${escapeHtml(item.id)}">Send test</button>`
        + `<button class="secondary farm-notify-open" data-id="${escapeHtml(item.id)}">Change</button>`
        + `<button class="secondary farm-notify-off" data-id="${escapeHtml(item.id)}">Remove</button></div>` : "")
      + `</div>`;
  }).join("");
  return renderSection(card, `${heading}<p class="muted">A rig's own channel is on its page: this is the farm telling you about the fleet.</p>`
    + `<div class="channel-cards">${cards}</div>`);
}

async function loadFarmNotify() {
  if (!shell().releases) return;
  try { farmNotify = await api("/api/v1/farm/notify"); } catch { farmNotify = null; }
  renderFarmNotify();
}

let farmNotifyEditing = false;
let farmNotifyKind = null;

function renderConfig(config) {
  const portal = Boolean(config?.portal);
  $("config-title").textContent = portal ? "What the portal decides with" : "What this rig decides with";
  renderFarmLink((config || {}).farm || {});
  const file = config?.portal?.config_file;
  $("config-source").innerHTML = portal
    ? (file ? `Set in <code>${escapeHtml(file)}</code>, read on each decision, without a restart.` : "No configuration is mounted on the portal: these are its defaults.")
    : "Set on this host: <code>sudo alteriom-hil-admin config set &lt;section.key&gt; &lt;value&gt;</code>, or <code>/etc/alteriom-hil/config.yaml</code>.";
  if (!config) {
    $("config").innerHTML = `<p class="failure-summary">The service returned no configuration. It may be running from a host provisioned before <code>/etc/alteriom-hil/config.yaml</code> existed.</p>`;
    return;
  }
  const q = config.quarantine || {}, r = config.retention || {}, n = config.notify || {}, b = config.backup || {};
  const groups = [
    viewGroup("Quarantine", "A board that keeps failing its Rig Health Check is kept out of runs.", [
      ["Automatic quarantine", onOff(q.enabled), "Released by a clean health check, or by an operator from the board's page."],
      q.enabled !== false ? ["After", plainValue(q.after_failures, "failed checks in a row")] : null,
    ]),
    viewGroup("Retention", "What is deleted on its own, daily. Reports, JUnit and records always stay.", [
      ["Delete old evidence", onOff(r.enabled), r.enabled ? "" : "Off: captures and logs are kept for good."],
      ["Serial and broker captures", plainValue(r.run_evidence_days, "days")],
      ["Job logs", plainValue(r.log_days, "days")],
      ["Newest runs always kept", plainValue(r.keep_newest_runs, "runs")],
      ["Stale checkouts", plainValue(r.workspace_days, "days")],
    ]),
    // One row per setting the *chosen* channel uses: a Telegram rig shown as
    // an enabled webhook with no format is a page describing something else.
    viewGroup("Notifications", "Where the farm says something broke.", (n.channel === "telegram" ? [
      ["Telegram", onOff(n.enabled), n.token_file ? `bot token read from ${n.token_file}` : ""],
      n.enabled ? ["Chat", plainValue(n.chat_id)] : null,
    ] : [
      ["Webhook", onOff(n.enabled), n.webhook_url_file ? `URL read from ${n.webhook_url_file}` : ""],
      n.enabled ? ["Format", plainValue(n.format)] : null,
    ]).concat([
      n.enabled ? ["Events", plainValue(n.events)] : null,
      ["Last delivery", plainValue(n.last_delivery)],
    ])),
    viewGroup("Backup", portal ? "The portal's job store, keys and bundles live on its volume." : "Nightly: the job database, registries, configuration and pinned bundles, never a secret.", [
      ["Nightly backup", onOff(b.enabled)],
      b.enabled ? ["Kept", plainValue(b.keep, "backups")] : null,
      b.enabled ? ["Copied to", plainValue(b.target)] : null,
      ["Last backup", plainValue(b.last_backup)],
    ]),
  ];
  if (!portal) {
    const s = config.service || {}, h = config.health || {};
    groups.push(
      viewGroup("Runs", `How ${site()} takes its work.`, [
        ["Runs at once", plainValue(s.concurrency)],
        ["A run may take", plainValue(s.suite_timeout_seconds, "s"), "Then it is interrupted, its evidence kept."],
      ]),
      viewGroup("Host health", "When this host calls itself degraded or unhealthy.", [
        ["Check every", plainValue(h.interval_minutes, "min")],
        ["Minimum boards", plainValue(h.minimum_boards, "boards")],
        ["Disk warning at", plainValue(h.disk_warn_percent, "%")],
        ["Disk critical at", plainValue(h.disk_critical_percent, "%")],
      ]),
    );
  }
  const hostSections = portal ? "" : renderConfigSections(config);
  $("config").innerHTML = `<div class="setting-groups">${groups.join("")}</div>${hostSections ? `<details class="host-config" data-keep="host-config"><summary>Everything this host is configured with <small class="muted">service, paths, rig network, broker, farm role</small></summary>${hostSections}</details>` : ""}`;
  // A person's Projects tab is drawn from their workspaces (loadProjects);
  // this is the farm's own view, for a rig and for a key.
  if (!(you?.account && shell().workspaceProjects)) renderFarmProjects(config.build || {});
}

// Who the projects panel was last drawn for. The first route can run before
// the status says who is looking, and would draw the farm's view for a
// person; the status handler redraws once it knows.
let projectsShownAs;

// Settings -> Farm connection, on a rig: whether this rig is connected to a
// farm portal, and how it would be. A rig works on its own; connecting it is
// the portal's Add rig, which hands back one command to run on this host.
function renderFarmLink(farm) {
  const card = $("farm-link");
  if (!card) return;
  const mode = farm.mode || "standalone";
  const portal = farm.portal_url ? `<a href="${escapeHtml(farm.portal_url)}" target="_blank" rel="noopener">${escapeHtml(farm.portal_url)}</a>` : "a farm";
  const body = mode === "standalone"
    ? `<p><span class="state">standalone</span> This rig is not connected to a farm. It runs its own suites, keeps its own history, and answers only here.</p>
       <p class="muted">To connect it: on your farm portal, <strong>Rigs → Add rig</strong> names this rig and hands you one command. Run that command on this host, as the user the rig runs as. The rig then takes runs from the portal as well, and its page there follows it. It keeps working here either way.</p>`
    : `<p><span class="state good">${escapeHtml(mode)}</span> Connected to ${portal}${farm.worker_name ? ` as <strong>${escapeHtml(farm.worker_name)}</strong>` : ""}.</p>
       <p class="muted">The portal hands this rig its runs and its releases; the node key that proves who it is stays on this host${farm.node_key_file ? ` (<code>${escapeHtml(farm.node_key_file)}</code>)` : ""}. To disconnect: <code>sudo alteriom-hil-admin config set farm.mode standalone && sudo alteriom-hil-admin config apply</code>; the portal's page for it shows it offline until it joins again.</p>`;
  card.innerHTML = `<div class="title-row"><div><p class="eyebrow">FARM</p><h2>Connection to a farm</h2></div></div>${body}`;
}

function farmProjectsMarkup(build) {
  const families = (build.targets || []).map(target => `<span class="artifact">${escapeHtml(target)}</span>`).join("");
  return `<div class="title-row"><div><p class="eyebrow">PROJECTS</p><h2>What the farm runs</h2></div><span class="muted">${escapeHtml(Object.keys(build.profile_details || {}).length)} profiles</span></div>
    <p class="muted">The farm does not build firmware: each project's bundles come from the workflow named here, and a run flashes one of them.</p>
    ${renderProfileTable(build.profile_details)}
    <h3>Chip families</h3><div class="artifacts">${families || '<span class="muted">none</span>'}</div>`;
}

function renderFarmProjects(build) {
  projectsShownAs = you?.name || "";
  $("config-projects").innerHTML = farmProjectsMarkup(build);
}

// Settings -> Projects. A signed-in person sees their own workspaces and the
// projects in each -- the repositories they test and what the farm runs for
// them -- not the farm's build configuration, which an account is handed as
// {} anyway. A rig, or a key, sees what this farm runs; so does whoever
// administers the platform, below their own, because the registry is theirs
// to keep.
async function loadProjects() {
  // Not before the first status: who is looking decides what this draws,
  // and drawing the farm's view first and swapping it a moment later is a
  // flash for a person and a wasted read for everybody.
  if (!document.body.dataset.mode) return;
  projectsShownAs = you?.name || "";
  if (!(you?.account && shell().workspaceProjects)) return shell().keyProjects();
  let page;
  try { page = await shell().workspaceProjects(); }
  catch (error) { $("config-projects").innerHTML = `<p class="failure-summary">${escapeHtml(error.message)}</p>`; return; }
  renderWorkspaceProjects(page.workspaces || []);
  if (isAdmin()) {
    try {
      const config = await api("/api/v1/config");
      $("config-projects").insertAdjacentHTML("beforeend", `<section class="card">${farmProjectsMarkup(config.build || {})}</section>`);
    } catch (error) { /* the farm's registry is not this page's reason to exist */ }
  }
}

// ---- Settings -> Projects, on a rig -------------------------------------------------
// What this rig runs, and where a person who just installed it adds their
// own: a project is their repository, its suite and the boards it wants.
// The rig writes the profile document under <state>/profiles/ and reads it
// back at once; the shipped ones are listed but are the release's to
// change.
let rigProjectsView = null;
let projectEditing = null;   // null, "new", or the name of the project being changed
let projectOpen = null;      // the name whose page is open below the table
let projectNotice = null;    // {tone, text} after a fetch, shown once
let projectDraft = null;     // what the rig found on GitHub for a project being added
let projectByHand = false;   // the person chose the long form over the look-up

async function loadRigProjects() {
  const card = $("config-projects");
  if (!card) return;
  try { rigProjectsView = await api("/api/v1/projects"); }
  catch (error) { card.innerHTML = `<p class="failure-summary">${escapeHtml(error.message)}</p>`; return; }
  renderRigProjects(rigProjectsView);
  renderGitHubCard(rigProjectsView.github);
}

// What this rig calls itself: a name (else its host's), a description, a
// location -- what its own page shows and, once connected, what a portal's
// page for it shows. Kept on the rig.
let rigDetailsEditing = false;
async function loadRigDetailsCard() {
  const card = $("rig-details-card");
  if (!card) return;
  let view;
  try { view = await api("/api/v1/rig/details"); }
  catch (error) { card.innerHTML = `<p class="failure-summary">${escapeHtml(error.message)}</p>`; return; }
  renderRigDetailsCard(view);
}

function renderRigDetailsCard(view) {
  const card = $("rig-details-card");
  if (!card) return;
  const shown = view.name || view.host;
  const body = rigDetailsEditing && isAdmin()
    ? `<form id="rig-details-own" class="settings-form" autocomplete="off">
        <label>Name <small class="muted">lowercase letters, digits, dots, underscores or hyphens; empty for the host's, <code>${escapeHtml(view.host)}</code></small><input name="name" value="${escapeHtml(view.name || "")}" pattern="[a-z0-9][a-z0-9._-]{0,31}" placeholder="${escapeHtml(view.host)}"></label>
        <label>Description <small class="muted">what this rig is for, in a line</small><input name="description" value="${escapeHtml(view.description || "")}" maxlength="200" placeholder="the bench under the window, alteriom firmware"></label>
        <label>Location <input name="location" value="${escapeHtml(view.location || "")}" maxlength="120" placeholder="Quebec, home lab"></label>
        <p id="rig-details-error" class="failure-summary" hidden></p>
        <div class="settings-footer"><button type="submit">Save</button><button type="button" class="secondary rig-details-cancel">Cancel</button></div>
      </form>`
    : `<div class="detail-grid">
        <div><small>Name</small><span><strong>${escapeHtml(shown)}</strong>${view.name ? "" : ' <small class="muted">the host\'s</small>'}</span></div>
        <div><small>Description</small><span>${view.description ? escapeHtml(view.description) : '<span class="muted">not set</span>'}</span></div>
        <div><small>Location</small><span>${view.location ? escapeHtml(view.location) : '<span class="muted">not set</span>'}</span></div>
      </div>`;
  card.innerHTML = `<div class="title-row"><div><p class="eyebrow">THIS RIG</p><h2>Name and place</h2></div>${!rigDetailsEditing && isAdmin() ? '<button type="button" class="secondary rig-details-edit">Change</button>' : ""}</div>
    <p class="muted">How this rig is called and where it is: on its own page, and on a farm's page for it once it is connected.</p>${body}`;
  card.querySelector(".rig-details-edit")?.addEventListener("click", () => { rigDetailsEditing = true; renderRigDetailsCard(view); });
  card.querySelector(".rig-details-cancel")?.addEventListener("click", () => { rigDetailsEditing = false; renderRigDetailsCard(view); });
  card.querySelector("#rig-details-own")?.addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const body = Object.fromEntries(new FormData(form).entries());
    try {
      const saved = await api("/api/v1/rig/details", {method: "POST", body: JSON.stringify(body)});
      rigDetailsEditing = false;
      renderRigDetailsCard(saved);
      if (rigPage.name === "local") showRig("local");
    } catch (error) {
      const note = form.querySelector("#rig-details-error");
      note.hidden = false;
      note.textContent = error.message;
    }
  });
}

async function loadGitHubCard() {
  if (!$("github-card") || !shell().projectsAreOwn) return;
  try { renderGitHubCard(await api("/api/v1/github")); }
  catch (error) { /* the card is a convenience; the Projects page says the same */ }
}

function projectRepoText(url) {
  return String(url || "").replace(/^https:\/\/(www\.)?(github\.com\/)?/, "").replace(/\.git$/, "");
}

// GitHub is the gate: a project is a GitHub repository whose CI builds what
// the rig flashes, so without a token the rig can add none, and the page
// says so with the command rather than offering a button that would be
// refused.
function githubKindText(github) {
  const kind = github.kind || "unknown";
  if (kind === "fine-grained") return "fine-grained token";
  if (kind === "classic") return "classic token";
  if (kind === "unknown") return "token";
  return `${escapeHtml(kind)} token`;
}

// When the token expires, as one phrase and a tone.
function githubExpiry(github) {
  if (!github.expires_at) {
    return github.kind === "classic" ? {text: "never (classic token)", tone: "muted"} : {text: "not stated", tone: "muted"};
  }
  const days = github.expires_in_days;
  const when = String(github.expires_at).slice(0, 10);
  if (typeof days === "number" && days < 0) return {text: `expired ${when}`, tone: "bad"};
  if (typeof days === "number" && days <= 14) return {text: `${when} · in ${days} day${days === 1 ? "" : "s"}`, tone: "warn"};
  return {text: typeof days === "number" ? `${when} · in ${days} days` : when, tone: "muted"};
}

// What a fine-grained token was given is its private repositories: GitHub
// shows every public repository to any token, so those are counted apart.
function githubGiven(github) {
  const repos = (github.access && github.access.repositories) || [];
  const given = repos.filter(row => row.private);
  return {given, publicRows: repos.filter(row => !row.private), more: Boolean(github.access && github.access.more)};
}

function githubFacts(github) {
  const facts = [];
  const set = github.source === "page" ? "from this page" : "on the host";
  if (github.connected) {
    const expiry = githubExpiry(github);
    const {given, publicRows, more} = githubGiven(github);
    facts.push(
      ["Status", '<span class="state good">connected</span>'],
      ["Account", `<strong>${escapeHtml(github.login)}</strong>`],
      ["Token", escapeHtml(githubKindText(github))],
      ["Expires", `<span class="${expiry.tone === "muted" ? "" : expiry.tone}">${escapeHtml(expiry.text)}</span>`],
      ["Set", set]);
    if (github.access) {
      facts.push(["Repositories", github.kind === "fine-grained"
        ? `${given.length} private given${publicRows.length ? ` · ${publicRows.length}${more ? "+" : ""} public` : ""}`
        : `all ${escapeHtml(github.login)} can see${(github.scopes || []).length ? ` · ${github.scopes.map(escapeHtml).join(", ")}` : ""}`]);
    }
  } else if (github.configured) {
    facts.push(
      ["Status", '<span class="state bad">refused</span>'],
      ["Token", escapeHtml(githubKindText(github))],
      ["Set", set],
      ["GitHub says", `<span class="bad">${escapeHtml(github.error || "no")}</span>`]);
  } else {
    facts.push(["Status", '<span class="state warn">not connected</span>']);
  }
  return `<div class="live-facts worker-facts github-facts">${facts.map(([label, value]) => `<div><small>${label}</small><span>${value}</span></div>`).join("")}</div>`;
}

// Per project: does the token see the repository, read its code, list its
// bundles -- the three things the rig does with it -- and what it needs if not.
function githubAccessMarkup(github) {
  const access = github.access;
  if (!access || !(access.projects || []).length) return "";
  const mark = ok => ok ? '<span class="state good">yes</span>' : '<span class="state bad">no</span>';
  const rows = access.projects.map(row => {
    const own = row.repo_access || {};
    const supply = row.supply_repo && row.supply_repo !== row.repo ? (row.supply_repo_access || {}) : own;
    const needs = !own.metadata ? "GitHub does not show it to this token"
      : !own.contents ? "Contents: read, to check the project out"
      : !supply.actions ? `Actions: read${row.supply_repo && row.supply_repo !== row.repo ? ` on ${projectRepoText(row.supply_repo)}` : ""}, to fetch its bundles` : "";
    return `<tr><td><strong>${escapeHtml(row.label || row.name)}</strong>${needs ? `<small class="warn">Needs ${escapeHtml(needs)}</small>` : ""}</td><td>${repoLink(row.repo)}</td><td>${mark(own.metadata)}</td><td>${mark(own.contents)}</td><td>${mark(supply.actions)}</td></tr>`;
  }).join("");
  return `<div class="table-wrap"><table class="fleet github-access"><thead><tr><th>Project</th><th>Repository</th><th>Sees it</th><th>Reads its code</th><th>Fetches its bundles</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

// The whole picture, behind Details: what a person opens when something was refused.
function githubDetailsMarkup(github) {
  const {given, publicRows} = githubGiven(github);
  const list = rows => rows.length ? `<p class="repo-list">${rows.map(row => `<code>${escapeHtml(row.name)}</code>`).join(" ")}</p>` : '<p class="muted">none</p>';
  const refusals = [];
  for (const row of (github.access && github.access.projects) || []) {
    for (const [field, acc] of [["repo", row.repo_access], ["supply_repo", row.supply_repo_access]]) {
      if (!acc || (field === "supply_repo" && row.supply_repo === row.repo)) continue;
      if (acc.error) refusals.push([row.label || row.name, acc.repo, acc.error]);
      for (const [what, why] of Object.entries(acc.refused || {})) refusals.push([row.label || row.name, acc.repo, `${what}: ${why}`]);
    }
  }
  const title = github.connected ? `${escapeHtml(github.login)} · ${escapeHtml(githubKindText(github))}` : github.configured ? "A token GitHub refuses" : "Not connected";
  return `<dialog class="dialog github-details"><div class="dialog-head"><div><p class="eyebrow">GITHUB TOKEN</p><h2>${title}</h2></div><button type="button" class="secondary dialog-close">Close</button></div>
    ${githubFacts(github)}
    ${github.kind === "fine-grained" && github.access ? `<h3>Private repositories it was given (${given.length})</h3>${list(given)}<p class="muted">A fine-grained token reaches the private repositories it was given and no other; GitHub shows every public repository to any token. Widen it on GitHub under the token's <em>Repository access</em>, then press Check again.</p><details><summary>Public repositories it sees (${publicRows.length}${github.access.more ? "+" : ""})</summary>${list(publicRows)}</details>` : ""}
    ${github.kind === "classic" ? `<h3>Scopes</h3><p>${(github.scopes || []).length ? github.scopes.map(scope => `<code>${escapeHtml(scope)}</code>`).join(" ") : '<span class="muted">none reported</span>'}</p><p class="muted">A classic token reaches everything its account can and does not expire. A fine-grained token with an expiry, given only the project repositories, is the safer kind for a rig.</p>` : ""}
    ${refusals.length ? `<h3>What GitHub refused</h3><ul class="plain">${refusals.map(([project, repo, why]) => `<li><strong>${escapeHtml(project)}</strong> · <code>${escapeHtml(projectRepoText(repo))}</code>: ${escapeHtml(why)}</li>`).join("")}</ul>` : ""}
    <h3>Where it lives</h3><p class="muted">${github.source === "page"
      ? `Given from this page; kept at <code>${escapeHtml(github.path || "")}</code>, readable by the rig only.`
      : `The host's file <code>${escapeHtml(github.path || "")}</code>, set with <code>${escapeHtml(github.how || "")}</code>. A token given from this page replaces it for the rig.`} Nothing shows the token itself.${github.access?.checked_at ? ` Checked ${escapeHtml(relativeWhen(github.access.checked_at))}.` : ""}</p>
  </dialog>`;
}

function githubMarkup(github) {
  if (!github) return "";
  lastGithubSummary = github;
  if (!github.connected && !github.configured) {
    return `${githubFacts(github)}<p class="muted">This rig has no GitHub token, so it can add no project: a project is a GitHub repository whose CI builds the firmware this rig flashes. A <strong>fine-grained</strong> token with <strong>Contents: read</strong> on the project repositories and <strong>Actions: read</strong> to fetch their bundles; the rig checks it with GitHub, keeps it beside its own key, and shows only who it is and what it reaches.</p>`;
  }
  return `${githubFacts(github)}${githubAccessMarkup(github)}`;
}

// The buttons: ask again, see everything, change the token, forget a page-given one.
function githubActions(github) {
  if (!github || !isAdmin() || !shell().projectsAreOwn) return "";
  const buttons = [];
  if (github.configured) buttons.push('<button type="button" class="secondary github-check">Check again</button>');
  if (github.configured) buttons.push('<button type="button" class="secondary github-details-open">Details</button>');
  buttons.push(`<button type="button" class="${github.configured ? "secondary " : ""}github-replace">${github.configured ? "Replace token" : "Connect GitHub"}</button>`);
  if (github.source === "page" && github.path) buttons.push('<button type="button" class="secondary github-forget">Forget it</button>');
  return `<div class="row-actions github-actions">${buttons.join("")}</div>`;
}

function githubTokenForm(github) {
  if (!isAdmin() || !shell().projectsAreOwn) return "";
  return `<form id="github-token-form" class="settings-form" autocomplete="off" hidden>
    <label>${github && github.configured ? "New token" : "GitHub token"} <small class="muted">a fine-grained token: Contents read and Actions read on the project repositories. It goes to this rig once, over this connection, and is not shown again.</small><input name="token" type="password" autocomplete="off" required placeholder="github_pat_…"></label>
    <p id="github-token-error" class="failure-summary" hidden></p>
    <div class="settings-footer"><button type="submit">${github && github.configured ? "Replace" : "Connect"}</button><button type="button" class="secondary github-cancel">Cancel</button></div>
  </form>`;
}

function githubCardMarkup(github) {
  return `${githubMarkup(github)}${githubActions(github)}${githubTokenForm(github)}${githubDetailsMarkup(github)}`;
}

function renderGitHubCard(github) {
  const card = $("github-card");
  if (!card) return;
  if (!github) { card.hidden = true; return; }
  card.hidden = false;
  card.innerHTML = `<div class="title-row"><div><p class="eyebrow">GITHUB</p><h2>Where projects come from</h2></div>${github.connected ? "" : '<a class="button secondary" href="#configuration/projects">Projects</a>'}</div>${githubCardMarkup(github)}`;
  wireGitHubCard(card);
}

function wireGitHubCard(card) {
  const form = card.querySelector("#github-token-form");
  const rerender = answer => {
    renderGitHubCard(answer.github);
    if (rigProjectsView) { rigProjectsView = {...rigProjectsView, github: answer.github}; renderRigProjects(rigProjectsView); }
  };
  card.querySelector(".github-replace")?.addEventListener("click", () => {
    if (!form) return;
    form.hidden = false;
    form.elements.token.focus();
  });
  card.querySelector(".github-cancel")?.addEventListener("click", () => {
    if (!form) return;
    form.hidden = true;
    form.elements.token.value = "";
    const note = card.querySelector("#github-token-error");
    if (note) note.hidden = true;
  });
  form?.addEventListener("submit", async event => {
    event.preventDefault();
    const note = card.querySelector("#github-token-error");
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      const answer = await api("/api/v1/github", {method: "POST", body: JSON.stringify({token: form.elements.token.value.trim()})});
      form.elements.token.value = "";
      rerender(answer);
    } catch (error) {
      note.hidden = false;
      note.textContent = error.message;
      button.disabled = false;
    }
  });
  card.querySelector(".github-check")?.addEventListener("click", async event => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "Asking GitHub…";
    try {
      rerender(await api("/api/v1/github/check", {method: "POST", body: "{}"}));
    } catch (error) { alert(`This rig refused: ${error.message}`); button.disabled = false; button.textContent = "Check again"; }
  });
  const dialog = card.querySelector("dialog.github-details");
  card.querySelector(".github-details-open")?.addEventListener("click", () => dialog?.showModal());
  dialog?.querySelector(".dialog-close")?.addEventListener("click", () => dialog.close());
  card.querySelector(".github-forget")?.addEventListener("click", async () => {
    if (!confirm("Forget the GitHub token this rig holds? It can add no project until it is given one again.")) return;
    try {
      rerender(await api("/api/v1/github/remove", {method: "POST", body: "{}"}));
    } catch (error) { alert(`This rig refused: ${error.message}`); }
  });
}

function projectOf(name) {
  return ((rigProjectsView || {}).projects || []).find(row => row.name === name) || null;
}

function renderRigProjects(view) {
  const card = $("config-projects");
  if (!card) return;
  const projects = view.projects || [];
  const github = view.github || null;
  const canAdd = Boolean(github && github.connected) && isAdmin();
  if (!canAdd && projectEditing === "new") projectEditing = null;
  if (projectOpen && !projectOf(projectOpen)) projectOpen = null;
  const originNote = row => row.origin === "changed" ? " · the release's, changed here" : row.origin === "shipped" ? " · shipped with the rig" : "";
  const rows = projects.map(row => `<tr class="clickable${projectOpen === row.name ? " selected" : ""}" data-project="${escapeHtml(row.name)}">
    <td><a class="row-link project-open" href="#configuration/projects/${escapeHtml(row.name)}"><strong>${escapeHtml(row.label || row.name)}</strong></a><small><code>${escapeHtml(row.name)}</code>${originNote(row)}</small></td>
    <td>${row.repo ? `<a href="${escapeHtml(row.repo)}" target="_blank" rel="noopener">${escapeHtml(projectRepoText(row.repo))}</a>` : '<span class="muted">—</span>'}<small>default <code>${escapeHtml(row.default_ref || "")}</code></small></td>
    <td>${escapeHtml(profileTakes(row))}</td>
    <td>${row.supply_workflow ? `<code>${escapeHtml(row.supply_workflow)}</code>` : '<span class="bad">no producer</span>'}${row.supply_repo && row.supply_repo !== row.repo ? `<small>${repoLink(row.supply_repo)}</small>` : ""}</td>
    <td><code>${escapeHtml(row.suite_path || "")}</code></td>
    <td class="nowrap"><button type="button" class="secondary project-open" data-name="${escapeHtml(row.name)}">${projectOpen === row.name ? "Close" : "Open"}</button></td>
  </tr>`).join("");
  const own = projects.filter(row => row.origin !== "shipped").length;
  const current = projectEditing && projectEditing !== "new" ? projects.find(row => row.name === projectEditing) : null;
  const gate = github && !github.connected ? `<section class="card project-gate">${githubCardMarkup(github)}</section>` : "";
  const removed = (view.removed || []).length
    ? `<p class="muted">Removed from this rig: ${view.removed.map(name => `<code>${escapeHtml(name)}</code>${isAdmin() ? ` <button type="button" class="secondary project-restore" data-name="${escapeHtml(name)}">Restore</button>` : ""}`).join(", ")}</p>`
    : "";
  const health = view.health_check
    ? `<p class="muted">The <strong>${escapeHtml(view.health_check.label)}</strong> is not a project: it is the rig's own firmware, installed with each release and run from <a href="#rigs">Boards</a>.</p>`
    : "";
  const notice = projectNotice ? `<p class="${projectNotice.tone === "bad" ? "failure-summary" : "muted"}">${escapeHtml(projectNotice.text)}${/GitHub/.test(projectNotice.text) ? ' <a href="#configuration">Open GitHub settings</a>' : ""}</p>` : "";
  projectNotice = null;
  card.innerHTML = `<div class="title-row"><div><p class="eyebrow">PROJECTS</p><h2>What this rig runs</h2></div><div class="row-actions"><span class="muted">${own ? `${own} of your own` : "none of your own yet"}</span>${canAdd && !projectEditing ? '<button type="button" class="secondary project-add">Add project</button>' : ""}${github && github.connected ? `<span class="muted" title="GitHub accepts this rig's token">GitHub · ${escapeHtml(github.login)}</span>` : ""}</div></div>
    <p class="muted">A project is a GitHub repository whose firmware this rig flashes and whose test suite it runs. This rig runs the projects listed here and no other: change or remove any of them, the reference the release ships included, and add your own. The rig does not build firmware: your project's CI builds a bundle, and this rig fetches it from GitHub or takes it when the CI hands it over; a run flashes it.</p>
    ${gate}${notice}
    ${projectEditing === "new" && !projectDraft && !projectByHand ? projectStartForm() : projectEditing ? projectForm(current) : ""}
    ${projects.length ? `<div class="table-wrap"><table class="fleet"><thead><tr><th>Project</th><th>Repository</th><th>Takes</th><th>Firmware from</th><th>Suite</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p class="muted">No project on this rig yet.</p>'}
    ${removed}${health}
    ${projectOpen ? projectPage(projectOf(projectOpen), github) : ""}
    <p class="muted">Your projects are documents under <code>${escapeHtml(view.directory || "")}</code>, one per project; upgrading the rig leaves them alone.</p>`;
  wireRigProjects(card);
  if (projectOpen) loadProjectPage(projectOpen);
}

// A project's page: everything the rig knows about it, the bundles it holds
// for it, its recent runs, and what can be done with it from here.
function projectPage(row, github) {
  if (!row) return "";
  const field = (label, value) => `<div><small>${label}</small><span>${value}</span></div>`;
  const needs = (row.needs || []).length
    ? row.needs.map(need => `${need.count > 1 ? `${need.count} × ` : ""}${escapeHtml(need.target)}${need.optional ? " (optional)" : ""}`).join(", ")
    : `the whole bench (at least ${escapeHtml(row.min_boards || 1)} board${(row.min_boards || 1) === 1 ? "" : "s"})`;
  const supply = row.supply_workflow
    ? `<code>${escapeHtml(row.supply_workflow)}</code> in ${row.supply_repo ? repoLink(row.supply_repo) : "its repository"}${row.supply_artifact ? `, artifact <code>${escapeHtml(row.supply_artifact)}</code>` : ""}`
    : '<span class="bad">no producer: nothing can give it a bundle</span>';
  const canFetch = Boolean(github && github.connected) && isAdmin() && Boolean(row.supply_workflow);
  const actions = [
    `<button type="button" class="secondary project-run" data-name="${escapeHtml(row.name)}">Run</button>`,
    canFetch ? `<button type="button" class="secondary project-fetch" data-name="${escapeHtml(row.name)}" title="Get the firmware your CI built: the newest bundle the supply workflow uploaded to GitHub, so a run can flash it">Get firmware from GitHub</button>` : "",
    isAdmin() ? `<button type="button" class="secondary project-edit" data-name="${escapeHtml(row.name)}">Change</button>` : "",
    isAdmin() ? `<button type="button" class="danger project-remove" data-name="${escapeHtml(row.name)}">${row.origin === "shipped" ? "Remove from this rig" : "Remove"}</button>` : "",
  ].filter(Boolean).join(" ");
  const eyebrow = row.origin === "shipped" ? "SHIPPED WITH THE RIG" : row.origin === "changed" ? "THE RELEASE'S, CHANGED HERE" : "YOUR PROJECT";
  return `<section class="card project-page" data-project="${escapeHtml(row.name)}">
    <div class="title-row"><div><p class="eyebrow">${eyebrow}</p><h2>${escapeHtml(row.label || row.name)}</h2></div><div class="row-actions">${actions}</div></div>
    ${canFetch ? '<p class="muted">The rig does not build firmware. <strong>Get firmware from GitHub</strong> fetches the newest bundle the project\'s supply workflow uploaded, so the run form has something to flash; your CI can also hand a bundle over directly.</p>' : ""}
    <div class="detail-grid">
      ${field("Name", `<code>${escapeHtml(row.name)}</code>`)}
      ${field("Repository", row.repo ? `<a href="${escapeHtml(row.repo)}" target="_blank" rel="noopener">${escapeHtml(projectRepoText(row.repo))}</a>` : "—")}
      ${field("Default ref", `<code>${escapeHtml(row.default_ref || "main")}</code>`)}
      ${field("Suite", `<code>${escapeHtml(row.suite_path || "")}</code>${row.location === "consumer" ? " <small class=\"muted\">checked out per run</small>" : " <small class=\"muted\">on this rig</small>"}`)}
      ${field("A run takes", needs)}
      ${field("A run may take", `${escapeHtml(shortDuration(row.timeout_seconds))}`)}
      ${field("Firmware from", supply)}
      ${field("Revision key", `<code>${escapeHtml(row.revision_key || "")}</code> <small class="muted">in the bundle's manifest</small>`)}
    </div>
    <div class="project-bundles"><p class="muted">Loading the bundles held for it…</p></div>
    <div class="project-runs"></div>
  </section>`;
}

async function loadProjectPage(name) {
  const page = document.querySelector(`.project-page[data-project="${CSS.escape(name)}"]`);
  if (!page) return;
  const bundlesBox = page.querySelector(".project-bundles");
  const runsBox = page.querySelector(".project-runs");
  const jobs = ((lastStatus || {}).jobs || []).filter(job => job.kind === "suite" && jobProfile(job) === name).slice(0, 5);
  const finished = ((lastStatus || {}).jobs || []).filter(job => job.kind === "suite" && jobProfile(job) === name && !["queued", "running"].includes(job.status)).length;
  runsBox.innerHTML = `<h3>Recent runs${finished && isAdmin() ? ` <button type="button" class="danger project-runs-delete" data-name="${escapeHtml(name)}" title="Delete this project's finished runs: their evidence, logs and records">Delete its runs</button>` : ""}</h3>${jobs.length
    ? `<div class="table-wrap"><table class="compact"><thead><tr><th>Run</th><th>Status</th><th>Revision</th><th>When</th></tr></thead><tbody>${jobs.map(job => `<tr class="clickable" data-href="#run/${escapeHtml(job.id)}"><td><a class="row-link" href="#run/${escapeHtml(job.id)}"><code>${escapeHtml(job.id.slice(0, 8))}</code></a></td><td><span class="state ${statusClass(job.status)}">${escapeHtml(job.status)}</span></td><td>${revisionLabel(job)}</td><td class="nowrap">${whenSpan(job.created_at)}</td></tr>`).join("")}</tbody></table></div>`
    : '<p class="muted">No run of this project yet.</p>'}`;
  linkRows(runsBox);
  runsBox.querySelector(".project-runs-delete")?.addEventListener("click", async event => {
    const button = event.currentTarget;
    if (!confirm(`Delete every finished run of ${name}? Their evidence, logs and records go; queued and running ones stay.`)) return;
    button.disabled = true;
    try {
      const answer = await api(`/api/v1/projects/${encodeURIComponent(name)}/runs/delete`, {method: "POST", body: "{}"});
      projectNotice = {tone: "good", text: `Deleted ${answer.deleted.length} run${answer.deleted.length === 1 ? "" : "s"} of ${name}${answer.kept.length ? `; ${answer.kept.length} still queued or running` : ""}.`};
      await refresh(true);
      renderRigProjects(rigProjectsView);
    } catch (error) { alert(`This rig refused: ${error.message}`); button.disabled = false; }
  });
  let found;
  try { found = await api(`/api/v1/artifacts?profile=${encodeURIComponent(name)}&limit=5`); }
  catch (error) { bundlesBox.innerHTML = `<p class="failure-summary">Could not list its bundles: ${escapeHtml(error.message)}</p>`; return; }
  const bundles = found.bundles || [];
  bundlesBox.innerHTML = `<h3>Bundles held <span class="muted">${escapeHtml(found.matched ?? bundles.length)}</span></h3>${bundles.length
    ? `<div class="table-wrap"><table class="compact"><thead><tr><th>Bundle</th><th>Revision</th><th>Families</th><th>From</th><th>Received</th></tr></thead><tbody>${bundles.map(bundle => `<tr class="clickable" data-href="#bundle/${escapeHtml(bundle.id)}"><td><a class="row-link" href="#bundle/${escapeHtml(bundle.id)}"><code>${escapeHtml(bundle.id.slice(0, 8))}</code></a>${bundle.pinned ? ' <span class="state good">pinned</span>' : ""}</td><td><code>${escapeHtml((bundle.revision || "").slice(0, 9))}</code>${bundle.branch ? ` <small class="muted">${escapeHtml(bundle.branch)}</small>` : ""}</td><td>${escapeHtml((bundle.families || []).map(f => f.family || f).join(", "))}</td><td class="muted">${escapeHtml(bundle.source?.kind === "release" ? "the release" : bundle.source?.run_id ? `run ${bundle.source.run_id}` : bundle.source?.kind || "")}</td><td class="nowrap">${whenSpan(bundle.received_at || bundle.modified)}</td></tr>`).join("")}</tbody></table></div>`
    : `<p class="muted">None yet. ${found.matched === 0 && projectOf(name)?.supply_workflow ? "Fetch the newest one the supply workflow built, or have that workflow hand it over." : ""}</p>`}`;
  linkRows(bundlesBox);
}

async function fetchProjectBundle(name, button) {
  button.disabled = true;
  button.textContent = "Fetching…";
  try {
    const answer = await api(`/api/v1/projects/${encodeURIComponent(name)}/fetch`, {method: "POST", body: "{}"});
    const which = answer.bundle?.id ? answer.bundle.id.slice(0, 8) : "";
    projectNotice = answer.fetched
      ? {tone: "good", text: `Fetched bundle ${which} from run ${answer.artifact?.run_id || ""} (${(answer.artifact?.commit || "").slice(0, 9)}${answer.artifact?.branch ? `, ${answer.artifact.branch}` : ""}). The run form offers it now.`}
      : {tone: "good", text: `The newest bundle (run ${answer.artifact?.run_id || ""}) is already held as ${which}; nothing new to fetch.`};
  } catch (error) {
    projectNotice = {tone: "bad", text: `Could not fetch a bundle: ${error.message}`};
  }
  renderRigProjects(rigProjectsView);
}

function runProject(name) {
  const select = $("profile-select");
  if (select && [...select.options].some(option => option.value === name)) select.value = name;
  navigateTo("#runs");
  $("run-card").open = true;
  renderProfiles();
  bringIntoView($("run-card"));
}

// A new project starts with the repository URL and nothing else: the rig
// looks it up on GitHub -- the default branch, the project's own
// .alteriom-hil.yaml when it has one, else the directory that looks like the
// suite and the workflow that looks like the HIL build -- and the form comes
// back filled in, with what was found and what was guessed said.
function projectStartForm() {
  return `<form id="project-start" class="settings-form" autocomplete="off">
    <div class="title-row"><div><p class="eyebrow">NEW PROJECT</p><h3>Start with the repository</h3></div></div>
    <label>Repository <small class="muted">on GitHub; this rig's token must be able to read it</small><input name="repo" type="url" required placeholder="https://github.com/you/my-sensor" autofocus></label>
    <p id="project-start-error" class="failure-summary" hidden></p>
    <div class="settings-footer"><button type="submit">Look it up</button><button type="button" class="secondary project-by-hand">Fill it in by hand</button><button type="button" class="secondary project-cancel">Cancel</button></div>
  </form>`;
}

function projectForm(current) {
  const draft = !current && projectDraft ? projectDraft : null;
  const source = current || (draft ? draft.suggested : null);
  const value = (key, fallback = "") => escapeHtml(source && source[key] != null ? source[key] : fallback);
  const families = current ? (current.needs || []).map(need => need.target) : (draft ? draft.suggested.families || [] : []);
  const found = draft ? `<p class="muted">${draft.found.length ? `Found in the repository: ${escapeHtml(draft.found.join(", "))}. ` : ""}${draft.guessed.length ? `Guessed: ${escapeHtml(draft.guessed.join("; "))}. ` : ""}${draft.private ? "The repository is private; this rig's token reads it. " : ""}${draft.taken ? `<span class="warn">A project named ${escapeHtml(draft.suggested.name)} exists here already; choose another name.</span> ` : ""}Check what follows and add.</p>` : "";
  return `<form id="project-form" class="settings-form" autocomplete="off" data-name="${current ? escapeHtml(current.name) : ""}">
    <div class="title-row"><div><p class="eyebrow">${current ? "CHANGE PROJECT" : "NEW PROJECT"}</p><h3>${current ? escapeHtml(current.label || current.name) : draft ? escapeHtml(draft.repo.replace(/^https:\/\/github\.com\//, "")) : "Your repository, its suite, its boards"}</h3></div></div>
    ${found}
    <label>Name <small class="muted">lowercase letters, digits and dashes; what a run names</small><input name="name" value="${value("name")}"${current ? " readonly" : ""} required pattern="[a-z0-9][a-z0-9-]{0,63}" placeholder="my-sensor"></label>
    <label>Label <small class="muted">how it reads on this page and in reports</small><input name="label" value="${value("label")}" placeholder="My sensor firmware"></label>
    <label>Repository <small class="muted">on GitHub, read with this rig's token; checked out fresh for every run</small><input name="repo" type="url" value="${value("repo")}" required pattern="https://github\\.com/.+" placeholder="https://github.com/you/my-sensor"></label>
    <label>Default ref <small class="muted">the branch or tag a run is for when none is named; empty takes the repository's default branch</small><input name="default_ref" value="${value("default_ref", "")}" placeholder="the repository's default branch"></label>
    <label>Suite path <small class="muted">a pytest suite, relative to the repository</small><input name="suite_path" value="${value("suite_path", "tests")}"></label>
    <label>Chip families <small class="muted">comma-separated; a run takes one board of each. Empty: the whole bench</small><input name="families" value="${escapeHtml(families.join(", "))}" placeholder="esp32, esp32-c3"></label>
    <label>Minimum boards <input name="min_boards" type="number" min="1" max="64" value="${value("min_boards", 1)}"></label>
    <label>A run may take <small class="muted">seconds, then it is interrupted and its evidence kept</small><input name="timeout_seconds" type="number" min="60" max="86400" value="${value("timeout_seconds", 1800)}"></label>
    <details><summary>Where its firmware comes from</summary>
      <p class="muted">This rig flashes what your CI built. Name the workflow that builds the bundle and the artifact it uploads; the bundle's manifest carries the commit under the revision key.</p>
      <label>Supply repository <small class="muted">empty: the repository above</small><input name="supply_repo" type="url" value="${value("supply_repo")}" placeholder="https://github.com/you/my-sensor"></label>
      <label>Workflow <input name="supply_workflow" value="${value("supply_workflow", ".github/workflows/hil.yml")}"></label>
      <label>Artifact name <input name="supply_artifact" value="${value("supply_artifact", "hil-artifacts")}"></label>
      <label>Manifest revision key <small class="muted">the key the bundle's manifest records the commit under</small><input name="revision_key" value="${value("revision_key", "git_sha")}" placeholder="git_sha"></label>
    </details>
    <p id="project-error" class="failure-summary" hidden></p>
    <div class="settings-footer"><button type="submit">${current ? "Save" : "Add project"}</button><button type="button" class="secondary project-cancel">Cancel</button></div>
  </form>`;
}

function wireRigProjects(card) {
  const redraw = () => renderRigProjects(rigProjectsView);
  card.querySelector(".project-add")?.addEventListener("click", () => {
    projectEditing = "new";
    projectDraft = null;
    projectByHand = false;
    redraw();
    ($("project-start") || $("project-form"))?.querySelector("input")?.focus();
  });
  card.querySelector(".project-by-hand")?.addEventListener("click", () => { projectByHand = true; redraw(); $("project-form")?.querySelector("input[name=name]")?.focus(); });
  card.querySelector("#project-start")?.addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button[type=submit]");
    const note = form.querySelector("#project-start-error");
    button.disabled = true;
    button.textContent = "Looking…";
    try {
      projectDraft = await api("/api/v1/projects/inspect", {method: "POST", body: JSON.stringify({repo: form.elements.repo.value.trim()})});
      redraw();
      $("project-form")?.querySelector("input[name=label]")?.focus();
    } catch (error) {
      note.hidden = false;
      note.textContent = error.message;
      button.disabled = false;
      button.textContent = "Look it up";
    }
  });
  card.querySelectorAll(".project-open").forEach(element => element.addEventListener("click", event => {
    event.preventDefault();
    const name = element.dataset.name || element.closest("tr")?.dataset.project;
    projectOpen = projectOpen === name && element.tagName === "BUTTON" ? null : name;
    history.replaceState(null, "", projectOpen ? `#configuration/projects/${projectOpen}` : "#configuration/projects");
    redraw();
  }));
  card.querySelectorAll(".project-run").forEach(button => button.addEventListener("click", () => runProject(button.dataset.name)));
  card.querySelectorAll(".project-restore").forEach(button => button.addEventListener("click", async () => {
    try {
      const answer = await api(`/api/v1/projects/${encodeURIComponent(button.dataset.name)}/restore`, {method: "POST", body: "{}"});
      rigProjectsView = {...rigProjectsView, projects: answer.projects, removed: answer.removed_shipped || []};
      projectOpen = answer.restored;
    } catch (error) { alert(`This rig refused: ${error.message}`); return; }
    redraw();
  }));
  wireGitHubCard(card);
  card.querySelectorAll(".project-fetch").forEach(button => button.addEventListener("click", () => fetchProjectBundle(button.dataset.name, button)));
  card.querySelectorAll(".project-edit").forEach(button => button.addEventListener("click", () => { projectEditing = button.dataset.name; redraw(); }));
  card.querySelectorAll(".project-cancel").forEach(button => button.addEventListener("click", () => { projectEditing = null; projectDraft = null; projectByHand = false; redraw(); }));
  card.querySelectorAll(".project-remove").forEach(button => button.addEventListener("click", async () => {
    const name = button.dataset.name;
    const row = projectOf(name);
    const shipped = row && row.origin !== "own";
    if (!confirm(shipped
      ? `Remove ${name} from this rig? It comes with the release, so it can be restored; its runs stay in the history.`
      : `Remove the project ${name}? Its runs stay in the history; this rig just stops offering it.`)) return;
    try {
      const answer = await api(`/api/v1/projects/${encodeURIComponent(name)}/delete`, {method: "POST", body: "{}"});
      rigProjectsView = {...rigProjectsView, projects: answer.projects, removed: answer.removed_shipped || []};
    } catch (error) { alert(`This rig refused: ${error.message}`); return; }
    projectEditing = null;
    if (projectOpen === name) projectOpen = null;
    redraw();
  }));
  const form = $("project-form");
  form?.addEventListener("submit", async event => {
    event.preventDefault();
    const fields = Object.fromEntries(new FormData(form).entries());
    const body = {
      ...fields,
      families: String(fields.families || "").split(",").map(part => part.trim()).filter(Boolean),
      min_boards: Number(fields.min_boards) || 1,
      timeout_seconds: Number(fields.timeout_seconds) || 1800,
    };
    for (const key of ["supply_repo", "revision_key", "label", "default_ref"]) if (!body[key]) delete body[key];
    const name = form.dataset.name;
    const path = name ? `/api/v1/projects/${encodeURIComponent(name)}` : "/api/v1/projects";
    const submit = form.querySelector("button[type=submit]");
    submit.disabled = true;
    try {
      const answer = await api(path, {method: "POST", body: JSON.stringify(body)});
      rigProjectsView = {...rigProjectsView, projects: answer.projects, removed: answer.removed_shipped || rigProjectsView.removed || []};
      projectEditing = null;
      projectDraft = null;
      projectByHand = false;
      projectOpen = answer.project?.name || projectOpen;
      redraw();
    } catch (error) {
      const note = $("project-error");
      note.hidden = false;
      note.textContent = error.message;
      submit.disabled = false;
    }
  });
}

// ---- the public farm, on a rig's overview ------------------------------------------
// The rig reads the farm's public page (GET /api/v1/farm/public, kept on
// the rig ten minutes at a time) and shows it: how many rigs and boards,
// which are shown publicly, how it is doing. Nothing here is this rig's;
// the point is that a rig on its own can see where it could connect.
let farmWorldAt = 0;

async function loadFarmWorld() {
  const card = $("farm-world");
  if (!card || !shell().farmWorld) return;
  if (Date.now() - farmWorldAt < 600000) return;
  farmWorldAt = Date.now();
  let view;
  try { view = await api("/api/v1/farm/public"); }
  catch (error) { farmWorldAt = 0; return; }
  renderFarmWorld(view);
}

function renderFarmWorld(view) {
  const card = $("farm-world");
  if (!card) return;
  if (!view || !view.url) { card.hidden = true; return; }
  card.hidden = false;
  const host = view.url.replace(/^https?:\/\//, "");
  const world = view.world;
  const stats = world?.stats || {};
  const item = (label, value) => `<div><small>${label}</small><strong>${value}</strong></div>`;
  const families = Object.entries(stats.families || {}).map(([family, count]) => `<span class="artifact">${escapeHtml(family)} × ${escapeHtml(count)}</span>`).join("");
  const rigRows = (world?.rigs || []).slice(0, 8).map(rig => `<tr>
    <td><strong>${escapeHtml(rig.name)}</strong><small>${escapeHtml([rig.description, rig.location].filter(Boolean).join(" · "))}</small></td>
    <td><span class="state ${rig.online ? "good" : "muted"}">${rig.online ? "online" : "offline"}</span></td>
    <td class="num">${escapeHtml(rig.boards ?? "—")}</td>
    <td>${escapeHtml(Object.entries(rig.families || {}).map(([family, count]) => `${count > 1 ? `${count} × ` : ""}${family}`).join(", ") || "—")}</td>
    <td class="num">${escapeHtml(rig.runs?.runs ?? 0)}${rig.runs?.runs ? ` <small class="muted">${escapeHtml(rig.runs.passed)} passed</small>` : ""}</td>
  </tr>`).join("");
  const connected = farmMode === "node" || farmMode === "attached";
  card.innerHTML = `<div class="title-row"><div><p class="eyebrow">THE FARM</p><h2><a href="${escapeHtml(view.url)}" target="_blank" rel="noopener">${escapeHtml(host)}</a></h2></div><div class="row-actions">${connected ? '<span class="state good">this rig reports to it</span>' : '<a class="button secondary" href="#configuration">Connect this rig</a>'}</div></div>
    ${world ? `<div class="stats-band-row">
      ${item("Rigs online", `${escapeHtml(stats.online ?? 0)} / ${escapeHtml(stats.rigs ?? 0)}`)}
      ${item("Boards", escapeHtml(stats.boards ?? 0))}
      ${item(`Runs, ${escapeHtml(world.window_days || 7)} days`, escapeHtml(stats.runs ?? 0))}
      ${item("Pass rate", escapeHtml(percent(stats.pass_rate)))}
    </div>
    ${families ? `<div class="artifacts">${families}</div>` : ""}
    ${rigRows
      ? `<div class="table-wrap"><table class="compact"><thead><tr><th>Public rig</th><th>State</th><th class="num">Boards</th><th>Families</th><th class="num">Runs</th></tr></thead><tbody>${rigRows}</tbody></table></div>`
      : '<p class="muted">No rig is shown publicly right now.</p>'}` : ""}
    <p class="muted">${view.ok
      ? `The farm's public page, as this rig read it ${whenSpan(view.fetched_at)}. Nothing here is this rig's; connecting it is a choice, made in Settings.`
      : `The farm could not be reached${view.error ? ` (${escapeHtml(view.error)})` : ""}${world ? "; this is what it last said" : ""}.`}</p>`;
}

function renderWorkspaceProjects(workspaces) {
  const projectRows = workspace => (workspace.projects || []).map(project => `<tr>
      <td><strong>${escapeHtml(project.label || project.name)}</strong>${project.label && project.label !== project.name ? `<small class="muted">${escapeHtml(project.name)}</small>` : ""}</td>
      <td>${project.repo ? `<a href="${escapeHtml(project.repo)}" rel="noreferrer">${escapeHtml(String(project.repo).replace(/^https:\/\//, ""))}</a>` : '<span class="muted">—</span>'}</td>
      <td>${project.suite_path ? `<code>${escapeHtml(project.suite_path)}</code>` : '<span class="muted">—</span>'}</td>
    </tr>`).join("");
  const card = workspace => {
    const repo = workspace.repo_url
      ? `<a href="${escapeHtml(workspace.repo_url)}" rel="noreferrer">${escapeHtml(workspace.repo_url.replace(/^https:\/\//, ""))}</a> <span class="state ${workspace.repo_visibility === "public" ? "good" : ""}">${escapeHtml(workspace.repo_visibility || "private")}</span>`
      : '<span class="muted">no repository set</span>';
    const rows = projectRows(workspace);
    return `<section class="card">
      <div class="title-row"><div><p class="eyebrow">WORKSPACE</p><h2>${escapeHtml(workspace.name)}</h2></div><span class="muted">${repo}</span></div>
      ${rows
        ? `<div class="table-wrap"><table class="compact"><thead><tr><th>Project</th><th>Repository</th><th>Suite</th></tr></thead><tbody>${rows}</tbody></table></div>`
        : `<p class="muted">No project yet. A project is the recipe for running your suite on your rigs: which repository, which ref, where the suite lives and how a bundle is flashed. For now a project is attached to a workspace by the platform; proving that a repository is yours, so you can attach one yourself, is what comes next.</p>`}
    </section>`;
  };
  $("config-projects").innerHTML = workspaces.length
    ? `<div class="title-row"><div><p class="eyebrow">YOUR PROJECTS</p><h2>What runs for you</h2></div><a class="secondary" href="#configuration/workspaces">Workspaces</a></div>${workspaces.map(card).join("")}`
    : `<div class="title-row"><div><p class="eyebrow">YOUR PROJECTS</p><h2>What runs for you</h2></div></div>
       <p class="muted">You have no workspace yet, so nothing runs for you. A workspace is a repository you test; one is made for you when a rig is given to you, and you can <a href="#configuration/workspaces">make one now</a>.</p>`;
  // A portal adds what a person may do here: add a project of their own.
  if (shell().afterWorkspaceProjects) shell().afterWorkspaceProjects($("config-projects"), workspaces);
}

// What a profile asks the bank for, by family. "3 board(s)" said how many and
// never which, so a family marked optional -- one whose board is off the rig
// for a while, which a run of this profile now goes without -- was visible
// only in the YAML on the host. This is where an operator sees it.
function profileTakes(spec) {
  if (spec.exclusive) return "the whole bank";
  const needs = spec.needs || [];
  if (!needs.length) return `${spec.min_boards || 1} board(s)`;
  return needs.map(need =>
    `${need.count > 1 ? `${need.count} x ` : ""}${need.target}` +
    `${(need.tags || []).length ? ` tagged ${need.tags.join(", ")}` : ""}` +
    `${need.optional ? " (optional)" : ""}`
  ).join(", ");
}

// What each profile is set to. The run form reads this to fill its picker;
// nothing showed it whole, so "what does this farm actually run, and where
// does its firmware come from" needed the YAML on the host.
function renderProfileTable(details) {
  const names = Object.keys(details || {}).sort();
  if (!names.length) return "";
  const rows = names.map(name => {
    const spec = details[name];
    const source = spec.repo
      ? `<a href="${escapeHtml(spec.repo)}" target="_blank" rel="noopener">${escapeHtml(spec.location)}</a>`
      : escapeHtml(spec.location || "");
    return `<tr><td><strong>${escapeHtml(spec.label || name)}</strong><small><code>${escapeHtml(name)}</code></small></td>` +
      `<td>${source}<small>default <code>${escapeHtml(spec.default_ref || "")}</code></small></td>` +
      `<td>${escapeHtml(profileTakes(spec))}</td>` +
      `<td>${spec.supply_workflow ? `<code>${escapeHtml(spec.supply_workflow)}</code>` : '<span class="bad">no producer</span>'}${spec.supply_repo ? `<small>${repoLink(spec.supply_repo)}</small>` : ""}</td>` +
      `<td><code>${escapeHtml(spec.suite_path || "")}</code></td></tr>`;
  }).join("");
  return `<div class="table-wrap"><table class="fleet"><thead><tr><th>Profile</th><th>Source</th><th>Takes</th><th>Firmware from</th><th>Suite</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

// Settings an operator will want to change from here rather than by editing
// /etc/alteriom-hil/config.yaml over ssh. Listed with what they do and what
// they currently are, and marked read-only, so the page is honest about being
// a view rather than pretending a control it does not have.
const PLANNED_CONTROLS = [
  ["Suite timeout", "service.suite_timeout_seconds", "How long a validation may run before the farm reclaims the rig."],
  ["Minimum boards", "health.minimum_boards", "Below this, the farm reports unhealthy instead of running a partial suite."],
  ["Disk thresholds", "health.disk_warn_percent", "When the host warns, and when it refuses new runs."],
  ["Health interval", "health.interval_minutes", "How often the snapshot behind the header badge is refreshed."],
  ["Default artifact families", null, "Which families a run selects when the operator does not choose."],
];

function renderPlannedControls(config) {
  const value = path => path ? path.split(".").reduce((o, k) => (o || {})[k], config) : undefined;
  $("planned-controls").innerHTML =
    `<div class="table-wrap"><table><thead><tr><th>Setting</th><th>Today</th><th>What it does</th></tr></thead><tbody>${
      PLANNED_CONTROLS.map(([label, path, what]) => {
        const now = value(path);
        return `<tr><td><strong>${escapeHtml(label)}</strong></td><td>${now === undefined ? '<span class="muted">not a setting yet</span>' : `<code>${escapeHtml(now)}</code>`}</td><td class="muted">${escapeHtml(what)}</td></tr>`;
      }).join("")}</tbody></table></div>
     <p class="muted">Changing these still means editing <code>/etc/alteriom-hil/config.yaml</code> and restarting the service. They are listed here so the gap is visible rather than assumed.</p>`;
}

async function loadConfig() {
  let config = null;
  try { config = await api("/api/v1/config"); renderConfig(config); renderPlannedControls(config); }
  catch (error) { $("config").innerHTML = `<p class="failure-summary">${escapeHtml(error.message)}</p>`; }
  const portal = Boolean(config?.portal) || shell().settingsPortal;
  // A host's controls are its file's; on a portal they are each rig's, and
  // changed on its page.
  $("planned-card").hidden = portal;
  if ($("portal-config")) $("portal-config").hidden = !portal;
  if (portal && config && $("portal-config")) renderPortalConfig(config);
}

// The three the dashboard has, plus whatever the shell adds -- Access
// stays last, because it is the admin's and reads as the end of the list.
function settingsTabs() {
  const tabs = shell().settingsOrder || ["general", "projects", ...shell().settingsTabs.map(tab => tab.id), "access"];
  // A person's Settings are their own: Workspaces, Projects, and whatever
  // else the shell adds for them. General is the farm's configuration and
  // Access its keys and audit -- farm-wide reads the service refuses an
  // account -- so they are not offered, the same way the nav hides them.
  return workspaceOnly() ? tabs.filter(name => name !== "general" && name !== "access") : tabs;
}

// The shell's tabs are put in the nav and given a panel to draw in, once,
// rather than written into index.html: the markup is the rig's bundle and
// what a portal adds is the portal's.
function buildSettingsTabs() {
  const nav = $("settings-tabs");
  const access = nav?.querySelector('[data-tab="access"]');
  if (!nav || !access) return;
  for (const tab of shell().settingsTabs) {
    if (nav.querySelector(`[data-tab="${tab.id}"]`)) continue;
    const link = document.createElement("a");
    link.href = `#configuration/${tab.id}`;
    link.dataset.tab = tab.id;
    link.textContent = tab.label;
    // The dashboard hides `.admin-only` for anybody who is not one; the
    // service refuses the route regardless.
    if (tab.admin) link.classList.add("admin-only");
    nav.insertBefore(link, access);
    const panel = document.createElement("div");
    panel.id = `settings-${tab.id}`;
    panel.hidden = true;
    $("settings-access").parentNode.insertBefore(panel, $("settings-access"));
  }
  // The shell may say the order outright: a person's own tabs first, the
  // administration last -- and the administration named as a group, so a
  // platform's settings, a person's settings on it and a rig's own are
  // never confused for one another.
  const order = shell().settingsOrder;
  if (order) {
    const links = [...nav.querySelectorAll("a[data-tab]")];
    links.sort((a, b) => order.indexOf(a.dataset.tab) - order.indexOf(b.dataset.tab)).forEach(link => nav.appendChild(link));
  }
  const group = shell().adminGroup || [];
  const first = group.map(id => nav.querySelector(`[data-tab="${id}"]`)).find(Boolean);
  if (first && !nav.querySelector(".tabs-group")) {
    const label = document.createElement("span");
    label.className = "tabs-group admin-only";
    label.textContent = "Administration";
    nav.insertBefore(label, first);
  }
}

function showSettingsTab(tab, updateHash = true) {
  buildSettingsTabs();
  const known = settingsTabs().includes(tab);
  settingsWanted = known || !tab ? null : tab;
  // The first tab this caller is offered: General for an operator, and for a
  // person the first of their own.
  settingsTab = known ? tab : settingsTabs()[0];
  if (settingsTab === "general" || settingsTab === "host") { loadFarmNotify(); shell().farmWebhooksLoad(); }
  for (const name of settingsTabs()) {
    const panel = $(`settings-${name}`);
    if (panel) panel.hidden = name !== settingsTab;
  }
  document.querySelectorAll("#settings-tabs a").forEach(link => link.classList.toggle("active", link.dataset.tab === settingsTab));
  if (updateHash) {
    history.replaceState(null, "", settingsTab === settingsTabs()[0] ? "#configuration" : `#configuration/${settingsTab}`);
    lastRouted = location.hash;
  }
  if (!token) return;
  if (settingsTab === "general") { loadConfig(); loadGitHubCard(); }
  if (settingsTab === "rig") { loadRigDetailsCard(); loadGitHubCard(); loadConfig(); }
  if (settingsTab === "host") loadConfig();
  if (settingsTab === "projects") loadProjects();
  if (settingsTab === "access" && isAdmin()) { loadAudit(); loadKeys(); }
  shell().settingsPanel(settingsTab);
}

// What a portal is set to: where its settings come from, and the worker
// protocol's clocks -- when a node counts as gone, when its runs are failed.
function renderPortalConfig(config) {
  const p = config.portal || {};
  const rows = [
    ["Public address", p.public_host ? escapeHtml(p.public_host) : '<span class="muted">not set</span>', "The links the portal puts in its notifications."],
    ["Rigs known", plainValue(p.workers)],
    ["A rig says it is there", p.heartbeat_seconds ? `every ${escapeHtml(p.heartbeat_seconds)} s` : plainValue(null)],
    ["Its boards leave the pool after", p.worker_stale_seconds ? `${escapeHtml(p.worker_stale_seconds)} s unheard` : plainValue(null)],
    ["Its runs fail as lost after", p.worker_lost_seconds ? `${escapeHtml(p.worker_lost_seconds)} s unheard` : plainValue(null)],
    ["A run nobody starts goes back after", p.lease_ack_seconds ? `${escapeHtml(p.lease_ack_seconds)} s` : plainValue(null)],
    ["Releases kept", plainValue(p.releases_kept)],
    ["Keys file", p.keys_file ? `<code>${escapeHtml(p.keys_file)}</code>` : plainValue(null)],
  ];
  const warning = p.config_file ? "" : `<p class="failure-summary">No configuration is mounted on the portal, so what it decides with is at its defaults: automatic quarantine and retention are off here, whatever a rig's own file says. The portal reads the file <code>ALTERIOM_HIL_CONFIG</code> names.</p>`;
  $("portal-config").innerHTML = `${warning}<div class="setting-groups">${viewGroup("Rigs and runs", "How the portal keeps track of its rigs. Fixed in the portal's code.", rows)}</div>`;
}

async function loadKeys() {
  let page;
  try { page = await api("/api/v1/keys"); }
  catch { return; }
  const card = $("keys");
  const rows = (page.keys || []).map(key => `<tr><td><strong>${escapeHtml(key.name)}</strong></td><td><span class="state ${key.role === "admin" ? "warn" : "good"}">${escapeHtml(key.role)}</span></td><td class="nowrap">${whenSpan(key.created_at)}</td><td class="muted">${escapeHtml(key.note || "")}</td></tr>`).join("");
  card.hidden = false;
  card.innerHTML = `<div class="title-row"><div><p class="eyebrow">ACCESS</p><h2>API keys</h2></div><span class="muted">Names and roles only; a key is shown once, when it is made</span></div>
    ${page.error ? `<p class="failure-summary">The keys file cannot be read: ${escapeHtml(page.error)}. Only the farm's own token works until it is fixed.</p>` : ""}
    ${rows ? `<div class="table-wrap"><table class="compact"><thead><tr><th>Name</th><th>Role</th><th>Made</th><th>Note</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p class="muted">No named keys: only the farm\'s own token.</p>'}
    <p class="muted">A <strong>node</strong> key is a rig's: it reaches only the worker routes, for the rig it is named for, and is made when the rig joins. A <strong>user</strong> key runs, checks and uploads; an <strong>admin</strong> key does everything.</p>`;
}

// Who changed what, newest first: the answer to "who deleted that bundle"
// that one shared token could never give.
async function loadAudit() {
  let page;
  try { page = await api("/api/v1/audit?limit=25"); }
  catch (error) { $("audit").innerHTML = `<p class="failure-summary">${escapeHtml(error.message)}</p>`; return; }
  const rows = (page.entries || []).map(entry => `<tr><td class="nowrap">${whenSpan(entry.at)}</td><td><strong>${escapeHtml(entry.key_name)}</strong> <small class="muted">${escapeHtml(entry.role)}</small></td><td><code>${escapeHtml(entry.method)} ${escapeHtml(entry.path)}</code></td><td><span class="state ${entry.status < 300 ? "good" : entry.status === 403 ? "warn" : "bad"}">${escapeHtml(entry.status)}</span></td></tr>`).join("");
  $("audit").innerHTML = rows
    ? `<div class="table-wrap"><table class="compact"><thead><tr><th>When</th><th>Key</th><th>Request</th><th>Answer</th></tr></thead><tbody>${rows}</tbody></table></div><p class="muted">${escapeHtml(page.total)} recorded in all.</p>`
    : '<p class="muted">Nothing has been changed through the API since the audit began.</p>';
}

// ---- Account -----------------------------------------------------------------
// Who you are here, how you signed in, and every browser that is still signed
// in as you. The header says your name and offers a sign out, but the header's
// whole .connection block is hidden below 850px -- so on a phone this page is
// the only way to see either, which is reason enough for it to exist.
async function loadAccount() {
  const identity = you;
  const account = identity && identity.account;
  if (!$("account-identity")) return;  // a rig's document has no account page
  if (!account) {
    // A pasted key is a key, not a person: it has no account behind it and
    // nothing here would be true of it.
    $("account-identity").innerHTML = '<p class="muted">This browser is using an API key rather than an account. Sign in with GitHub or an emailed link to have one.</p>';
    $("account-sessions").innerHTML = "";
    return;
  }
  const ways = [
    account.github_login ? `<div><small>GitHub</small><span>${escapeHtml(account.github_login)}</span></div>` : "",
    account.email ? `<div><small>Email</small><span>${escapeHtml(account.email)}</span></div>` : "",
  ].filter(Boolean).join("");
  $("account-identity").innerHTML = `<div class="detail-grid">
    <div><small>Handle</small><span>${escapeHtml(account.handle)}</span></div>
    <div><small>Role</small><span class="state ${identity.role === "admin" ? "good" : identity.role === "guest" ? "warn" : ""}">${escapeHtml(identity.role)}</span></div>
    ${ways}
    <div><small>Account since</small><span>${whenSpan(account.created_at)}</span></div>
  </div>${identity.role === "guest"
    ? '<p class="muted">You are signed in but not yet let in. The farm\'s admin names who may read it.</p>'
    : ways.split("<div>").length > 2
      ? '<p class="muted">Both ways in reach this one account: signing in either way is the same you.</p>'
      : ""}`;
  await loadSessions();
  // A portal adds what it keeps for a person besides sessions: GitHub
  // connected for repositories.
  if (shell().afterAccount) shell().afterAccount(account);
}

async function loadSessions() {
  let page;
  try { page = await api("/api/v1/sessions"); }
  catch (error) { $("account-sessions").innerHTML = `<p class="failure-summary">${escapeHtml(error.message)}</p>`; return; }
  const sessions = page.sessions || [];
  const rows = sessions.map(session => `<tr>
    <td>${session.current ? '<strong>This browser</strong>' : '<span class="muted">Another browser</span>'}</td>
    <td class="nowrap">${whenSpan(session.last_seen_at || session.created_at)}</td>
    <td class="nowrap">${whenSpan(session.expires_at)}</td>
    <td>${session.address ? `<code>${escapeHtml(session.address)}</code>` : '<span class="muted">—</span>'}</td>
    <td class="num"><button class="secondary revoke-session" data-id="${escapeHtml(session.id)}">${session.current ? "Sign out" : "Revoke"}</button></td>
  </tr>`).join("");
  const others = sessions.filter(session => !session.current).length;
  $("account-sessions").innerHTML = `<div class="table-wrap"><table class="compact"><thead><tr><th>Where</th><th>Last seen</th><th>Expires</th><th>Address</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="row-actions">
      ${others ? '<button class="secondary revoke-session" data-id="others">Sign out everywhere else</button>' : ""}
      <p class="muted">A sign-in lasts ${escapeHtml(page.days)} days. Revoking one takes effect at once.</p>
    </div>`;
  $("account-sessions").querySelectorAll(".revoke-session").forEach(button => button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const result = await api("/api/v1/sessions/revoke", {method: "POST", body: JSON.stringify({id: button.dataset.id})});
      // Revoking the browser you are using is a sign-out, and the cookie is
      // already gone: reload rather than leave a page that cannot refresh.
      if (result.current) { token = ""; sessionStorage.removeItem("farmToken"); location.href = "/"; return; }
      await loadSessions();
    } catch (error) { alert(error.message); button.disabled = false; }
  }));
}

// ---- Live pipeline -----------------------------------------------------------
// The running job with a clock that ticks, the stage it is in and how long
// that stage has taken, the boards it holds, and the queue behind it in the
// order the worker will take it — with the controls: cancel, run next,
// pause and resume the queue. With queue.concurrency above one, the other
// runs sharing the rig are listed under the first, and every queued job says
// why it has not started.
// A running job as the live pipeline shows it -- the clock, the stage, the
// boards it holds, the stage strip and progress -- with any other run sharing
// its rig listed under it. The overview and a rig's page both show it.
function liveRunColumn(running, alsoRunning, inv) {
  const progress = inferredProgress(running);
  const done = progress.filter(stage => ["passed", "skipped"].includes(stage.status)).length;
  const current = progress.find(stage => stage.status === "running");
  const held = (inv?.boards || []).filter(board => board.held_by?.job_id === running.id);
  return `
      <div class="title-row"><div><p class="eyebrow">LIVE PIPELINE</p><h2>${escapeHtml(running.kind)} · ${escapeHtml(running.id.slice(0, 8))}</h2></div><span class="state warn">${escapeHtml(running.status)}</span></div>
      <div class="live-facts">
        <div><small>Running for</small>${liveTimer(running.started_at, "timer")}</div>
        <div><small>Current stage</small><span>${escapeHtml(current?.label || (done === progress.length ? "Finishing" : "Starting"))}${current?.started_at ? ` · ${liveTimer(current.started_at)}` : ""}</span></div>
        <div><small>Project</small><span>${escapeHtml(jobProject(running))}</span></div>
        <div><small>Repository</small><span>${repoLink(jobRepo(running)) || "—"}</span></div>
        <div><small>Revision</small><span>${revisionLabel(running)}</span></div>
        <div><small>Boards held</small><span>${held.length ? `${held.length}: ${escapeHtml(held.map(b => b.id).join(", "))}` : running.kind === "build" ? "none (build only)" : "—"}</span></div>
        <div><small>Waited in queue</small><span>${escapeHtml(formatDuration(running.queued_seconds))}</span></div>
        <div><small>Started</small><span>${running.started_at ? escapeHtml(new Date(running.started_at).toLocaleTimeString()) : "—"}</span></div>
      </div>
      <div class="stage-strip">${progress.map(stage => `<span class="${escapeHtml(stage.status)}" title="${escapeHtml(stage.summary || "")}">${escapeHtml(stage.label)}</span>`).join("")}</div>
      <div class="progress-track"><span></span></div>
      <p class="muted">${escapeHtml(current?.summary || jobSummary(running))}</p>
      <div class="actions"><button class="secondary active-view" data-id="${escapeHtml(running.id)}">Follow execution</button>${jobActionButtons(running)}</div>
      ${alsoRunning.length ? `<div class="also-running"><p class="eyebrow">ALSO RUNNING</p><ol class="queue-list">${alsoRunning.map(job => {
        const boards = (inv?.boards || []).filter(board => board.held_by?.job_id === job.id).map(board => board.id);
        const stage = inferredProgress(job).find(item => item.status === "running");
        return `<li><span class="queue-pos">▶</span><div><strong>${escapeHtml(job.kind)} · ${escapeHtml(job.id.slice(0, 8))}</strong><small>${escapeHtml(jobProject(job))} · ${escapeHtml(stage?.label || "Starting")} · ${liveTimer(job.started_at)}</small><small>${boards.length ? `Boards: ${escapeHtml(boards.join(", "))}` : "No boards held"}</small></div><div class="actions"><button class="secondary active-view" data-id="${escapeHtml(job.id)}">Follow</button>${jobActionButtons(job)}</div></li>`;
      }).join("")}</ol></div>` : ""}
    `;
}

function renderActive(jobs, queue, inv) {
  if (!$("active-run")) return;   // on a rig the live run is the rig page's (renderRigLive)
  const byId = new Map(jobs.map(job => [job.id, job]));
  const runningJobs = (queue?.running_jobs || jobs.filter(job => job.status === "running").map(job => job.id)).map(id => byId.get(id)).filter(Boolean);
  const running = runningJobs[0];
  const alsoRunning = runningJobs.slice(1);
  const queued = (queue?.queued || []).map(id => byId.get(id)).filter(Boolean);
  const waiting = queue?.waiting || {};
  const pauseButton = `<button class="secondary queue-toggle admin-only" data-paused="${queue?.paused ? "1" : "0"}" title="${queue?.paused ? "Let the next queued job start" : "Finish the running job, then start nothing until resumed"}">${queue?.paused ? "Resume queue" : "Pause queue"}</button>`;
  // Why it is paused, when it was not an operator who paused it: a queue
  // paused for no stated reason is one somebody resumes without knowing what
  // it was protecting.
  const pausedNote = queue?.paused ? `<span class="queue-paused">Paused${queue.paused_since ? ` since ${new Date(queue.paused_since).toLocaleTimeString()}` : ""} — nothing queued will start${queue.paused_reason ? `: ${escapeHtml(queue.paused_reason)}` : ""}</span>` : `<span class="muted">${queued.length ? `${queued.length} waiting` : "Nothing waiting"}</span>`;
  const queueList = queued.length
    ? `<ol class="queue-list">${queued.map((job, index) => `<li><span class="queue-pos">${index + 1}</span><div><strong>${escapeHtml(job.kind)} · ${escapeHtml(job.id.slice(0, 8))}</strong><small>${escapeHtml(jobProject(job))} · ${revisionLabel(job)} · waited ${liveTimer(job.created_at)}</small>${waiting[job.id] ? `<small class="queue-wait">${escapeHtml(waiting[job.id])}</small>` : ""}</div><div class="actions">${jobActionButtons(job, {promotable: index > 0})}</div></li>`).join("")}</ol>`
    : "";
  const queueBlock = `<div><div class="queue-head"><div><p class="eyebrow">QUEUE</p>${pausedNote}</div>${pauseButton}</div>${queueList}</div>`;
  if (!running) {
    $("active-run").innerHTML = `<div class="live-grid"><div><div class="title-row"><div><p class="eyebrow">PIPELINE</p><h2>No active execution</h2></div><span class="state ${queue?.paused ? "warn" : "good"}">${queue?.paused ? "PAUSED" : "READY"}</span></div><p class="muted">${queue?.paused ? "The queue is paused; resume it to start the next job." : `${Site()} is ready to accept a validation run.`}</p></div>${queueBlock}</div>`;
  } else {
    const progress = inferredProgress(running);
    const done = progress.filter(stage => ["passed", "skipped"].includes(stage.status)).length;
    $("active-run").innerHTML = `<div class="live-grid"><div>${liveRunColumn(running, alsoRunning, inv)}</div>${queueBlock}</div>`;
    setProgress($("active-run"), progress.length ? done / progress.length : 0);
    $("active-run").querySelectorAll(".active-view").forEach(button => button.addEventListener("click", event => showJob(event.currentTarget.dataset.id, {focus: true, force: true})));
  }
  installJobActionHandlers($("active-run"));
  $("active-run").querySelectorAll(".queue-toggle").forEach(button => button.addEventListener("click", async () => {
    button.disabled = true;
    try { await api(button.dataset.paused === "1" ? "/api/v1/queue/resume" : "/api/v1/queue/pause", {method: "POST", body: "{}"}); await refresh(true); }
    catch (error) { alert(`Queue control failed: ${error.message}`); button.disabled = false; }
  }));
}


async function refresh(force = false) {
  if (pollInFlight || !token || document.hidden) return;
  pollInFlight = true;
  try {
    const data = await api("/api/v1/status");
    $("login").hidden = true; $("dashboard").hidden = false;
    if ($("console").hidden) setUpConsole();
    const signature = JSON.stringify(data);
    if (force || signature !== statusSignature) {
      statusSignature = signature; lastStatus = data; const inv = data.inventory || {}, jobs = data.jobs || [];
      inv.targets = data.targets || [];
      lastQueue = data.queue || {paused: false, queued: []};
      renderYou(data.you);
      farmMode = data.mode || "standalone";
      workers = data.workers || [];
      pendingRigs = data.pending_rigs || [];
      currentRelease = data.release || null;
      portalUrl = data.portal_url || null;
      document.body.dataset.mode = farmMode;
      // Which shell this is is known now, so a settings tab that was asked
      // for before anything knew it existed can be shown.
      if (settingsWanted) showSettingsTab(settingsWanted);
      // The shell's own tabs, which the first route built for a rig (none):
      // idempotent, so asking again once the shell is known costs nothing.
      buildSettingsTabs();
      // And the Projects panel, if it was drawn before anybody knew whose
      // page this is (a deep link runs its first route before the status).
      if (settingsTab === "projects" && projectsShownAs !== (you?.name || "")) loadProjects();
      repositories = data.repositories || repositories;
      defaultProfile = data.default_profile || defaultProfile;
      const hadProfiles = Object.keys(profiles).length > 0;
      profiles = data.profiles || profiles;
      // A bundle's page opened from a link renders before the profiles have
      // arrived, and whether it can be run depends on them.
      if (!hadProfiles && bundlePage.entry) renderBundlePage();
      hasActiveJob = jobs.some(job => ["queued", "running"].includes(job.status));
      rigBusy = jobs.some(job => ["queued", "running"].includes(job.status) && ["suite", "build"].includes(job.kind));
      rigAlone = (inv.reservations || []).some(held => !held.shared);
      lastInventory = inv;
      const health = data.health?.status || "unknown"; $("overall").textContent = health.toUpperCase(); $("overall").className = `badge ${statusClass(health)}`;
      // What is behind a badge that is not OK, without opening a page: on a
      // portal, which node is offline, behind or failing its install.
      $("overall").title = (data.health?.checks || []).filter(check => check.status !== "ok").map(check => `${check.name}: ${check.message}`).join("\n");
      renderNodeBanner();
      renderProfiles(); renderVersion(data.version, repositories); renderOverview(data); renderFleet();
      if (shell().overviewIsRigPage && rigPage.name === "local" && document.querySelector('.page[data-page="rig"].active')) showRig("local"); renderFamilies(data.targets || [], inv); renderSuiteTests(data.suite_tests || []); renderActive(jobs, lastQueue, inv); loadJobs();
      // An open rig or board page follows the poll too.
      if (!document.querySelector('.page[data-page="rig"]').hidden && rigPage.name && !rigPage.busy.size) showRig(rigPage.name);
      if (!document.querySelector('.page[data-page="board"]').hidden && boardPage.id) renderBoard();
    }
    loadOverviewStats();
    loadFarmWorld();
    if (selectedJobId) await showJob(selectedJobId);
    $("live-dot").className = "live-dot online"; $("last-updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    $("live-dot").className = "live-dot offline"; $("last-updated").textContent = "Connection lost";
    if (!$("dashboard").hidden) return;
    $("login").hidden = false; $("dashboard").hidden = true; $("overall").textContent = "LOCKED";
  } finally { pollInFlight = false; scheduleRefresh(); }
}

function scheduleRefresh() {
  clearTimeout(pollTimer); if (!token || document.hidden) return;
  pollTimer = setTimeout(() => refresh(), hasActiveJob ? 3000 : 15000);
}

// Configuration is fetched when its tab is opened rather than on every poll:
// it changes when an operator changes it, not every fifteen seconds.
document.querySelectorAll(".nav-item,.go-panel").forEach(button => button.addEventListener("click", () => {
  showPanel(button.dataset.panel);
  // A nav item may name the tab it opens: the rig's Boards is the rigs
  // page on its boards tab.
  if (button.dataset.panel === "rigs") showRigsTab(button.dataset.tab || rigsTab);
  if (button.dataset.panel === "configuration") showSettingsTab(settingsTab);
  if (button.dataset.panel === "artifacts" && token) showFirmwareTab(firmwareTab);
  if (button.dataset.panel === "statistics" && token) loadStatistics(statsDays);
  if (button.dataset.panel === "account" && token) loadAccount();
}));
$("token-form").addEventListener("submit", event => {
  event.preventDefault(); token = $("token").value; sessionStorage.setItem("farmToken", token);
  // Whatever the address named loads now. Routing ran before there was a
  // token and skipped every request, and only a plain #artifacts used to be
  // picked up again here -- so a shared link to a run, a bundle or a storage
  // page stayed empty after logging in, until a reload.
  refresh(true).then(() => routeFromLocation({force: true}));
});
$("close-rig").addEventListener("click", () => navigateTo("#rigs"));
$("close-board").addEventListener("click", () => navigateTo(boardPage.rig ? rigHref(boardPage.rig) : "#boards"));
installRigPageHandlers();
// The fleet overview's button; a rig's document, whose overview is its rig page, has none.
$("overview-new-run")?.addEventListener("click", () => { navigateTo("#runs"); $("run-card").open = true; bringIntoView($("run-card")); });
$("board-search").addEventListener("input", event => { boardFilter.q = event.target.value; renderBoardsTable(); });
$("board-rig").addEventListener("change", event => { boardFilter.rig = event.target.value; renderBoardsTable(); });
$("board-family").addEventListener("change", event => { boardFilter.family = event.target.value; renderBoardsTable(); });
$("board-state").addEventListener("change", event => { boardFilter.state = event.target.value; renderBoardsTable(); });
$("close-job").addEventListener("click", () => {
  selectedJobId = null; selectedJob = null; detailSignature = "";
  showPanel("runs");
});
// The farm checking its own hardware with its own firmware: the canary is
// flashed onto the boards asked about and every check is run on each. It is
// an ordinary queued run, so it waits for the rig rather than being refused
// while it is busy -- and it overwrites whatever firmware those boards are
// carrying, which is why it asks.
async function requestHealth(boards, button) {
  const what = boards ? `${boards.length} board(s)` : "every connected board";
  if (!confirm(`Run the Rig Health Check on ${what}?\n\nThe Rig Health Check is the rig's own firmware: it checks that the ESP boots, its serial path is clean, its flash keeps a value, the rig can reset it, its radio sees and joins the rig, and the rig's uplink and broker answer.\n\nIt flashes over whatever firmware ${boards ? "that board is" : "those boards are"} carrying now.`)) return;
  if (button) button.disabled = true;
  try {
    const job = await api("/api/v1/health", {
      method: "POST",
      body: JSON.stringify(boards ? {boards} : {}),
    });
    openConsole(rigPage.name && rigPage.name !== "local" ? rigPage.name : undefined);
    await loadJobs();
    await showJob(job.id, {focus: true});
  } catch (error) {
    alert(`Health check failed to queue: ${error.message}`);
  } finally {
    if (button) button.disabled = false;
  }
}

$("check-all").addEventListener("click", () => requestHealth(null, $("check-all")));

$("refresh").addEventListener("click", async () => {
  const button = $("refresh"); button.disabled = true; button.textContent = "Discovering…";
  try {
    const queued = await api("/api/v1/inventory/refresh", {method: "POST", body: "{}"});
    for (let attempt = 0; attempt < 240; attempt++) { await new Promise(resolve => setTimeout(resolve, 500)); const job = await api(`/api/v1/jobs/${queued.id}`); if (!["queued", "running"].includes(job.status)) { if (job.status === "failed") throw new Error(job.result?.error || job.result?.summary || "discovery failed"); break; } }
    await refresh(true);
  } catch (error) { alert(`Rediscover failed: ${error.message}`); }
  finally { button.disabled = rigBusy; button.textContent = "Rediscover devices"; }
});
$("suite-form").addEventListener("submit", async event => {
  event.preventDefault(); const form = new FormData(event.target), targets = form.getAll("target");
  const bundle = selectedBundle();
  if (!bundle) return alert(`Choose a firmware bundle. ${Site()} does not build firmware: it flashes a bundle a project's CI built.`);
  if (!targets.length) return alert("Select at least one artifact family");
  // The bundle is the whole of what is flashed, so it names the commit too.
  const body = {profile: form.get("profile"), ref: bundle.revision, artifact: bundle.id, targets};
  if (bundle.branch && /^[A-Za-z0-9][A-Za-z0-9._\/-]{0,127}$/.test(bundle.branch)) body.branch = bundle.branch;
  const tests = form.getAll("test");
  if (tests.length) body.tests = tests;
  const keyword = (form.get("keyword") || "").trim();
  if (keyword) body.keyword = keyword;
  try {
    const job = await api("/api/v1/suites", {method: "POST", body: JSON.stringify(body)});
    preferredBundle = null;
    await refresh(true); await showJob(job.id, {focus: true, force: true});
  } catch (error) { alert(`${Site()} refused the run: ${error.message}`); }
});
$("suite-form").elements.keyword.addEventListener("input", updateSelectionNote);
$("profile-select").addEventListener("change", () => renderProfiles());
$("bundle-select").addEventListener("change", () => { preferredBundle = null; renderFamilies(familyArgs.targets, familyArgs.inv); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) { refresh(true); tickDurations(); } else clearTimeout(pollTimer); });
// The URL is the state: a reload, a pasted link and the browser's back
// button all land on the same page, showing the same run.
routeFromLocation({force: true});
// Back and Forward arrive as `popstate`; a click on a plain `#…` link as a
// `hashchange` everywhere and a `popstate` too in some browsers. Either way
// the address is routed once.
window.addEventListener("popstate", () => routeFromLocation());
window.addEventListener("hashchange", () => routeFromLocation());
// A person who signed in with GitHub or by email has a session cookie and
// no key to paste: ask the farm who they are before showing the login card.
// A key pasted earlier in this tab still wins, as it did.
async function bootSession() {
  const query = new URLSearchParams(location.search);
  const said = query.get("signin");
  if (said) {
    history.replaceState(null, "", location.pathname + location.hash);
    showSignInNote(said === "expired"
      ? "That link has been used, or has expired: ask for another."
      : "Signing in did not work. Try again, or ask for a link by email.");
  }
  if (token && token !== "session") {
    // A key this tab kept may have been revoked since, or mistyped: shown
    // the login card with GitHub and email hidden, a person could only
    // paste another key. Ask the farm once; a key it refuses is let go.
    const accepted = await fetch("/api/v1/whoami", {headers: {"Authorization": `Bearer ${token}`}})
      .then(answer => answer.ok).catch(() => false);
    if (accepted) return refresh(true);
    token = "";
    sessionStorage.removeItem("farmToken");
    showSignInNote("The key this tab had is no longer accepted. Sign in, or connect with another key.");
  }
  if (!shell().accounts) return;
  try {
    const answer = await fetch("/api/v1/whoami", {credentials: "same-origin"});
    if (!answer.ok) throw new Error("nobody");
    const who = await answer.json();
    if ($("sign-out")) $("sign-out").hidden = false;
    if (who.role === "guest") {
      // Signed in, and not let in: the farm has not opened this account.
      // The card stays, with the ways in hidden and the reason said, so
      // the person knows where they stand rather than seeing a locked
      // dashboard fail to load.
      renderYou(who);
      if ($("signin-methods")) $("signin-methods").hidden = true;
      $("signin-key").hidden = true;
      showSignInNote(`You are signed in as ${who.name}. This farm has not opened your account yet: the person who runs it can, and you are in on your next sign-in.`);
      return;
    }
    token = "session";
    await refresh(true);
    routeFromLocation({force: true});
  } catch {
    loadSignInOptions();
  }
}

function showSignInNote(text) {
  const note = $("signin-note");
  if (!note) return;
  note.textContent = text || "";
  note.hidden = !text;
}

// Which ways in this portal offers: GitHub and email when they are set up,
// the key always, opened on its own when it is the only way.
async function loadSignInOptions() {
  // A rig signs nobody in: its key is the way, and its document has no
  // other. The portal's document has GitHub and email.
  if (!shell().accounts || !$("signin-methods")) return;
  let options = {github: false, email: false};
  try { options = await (await fetch("/auth/options")).json(); } catch {}
  const back = location.pathname + location.hash;
  $("signin-github").hidden = !options.github;
  $("signin-github").href = "/auth/github?next=" + encodeURIComponent(back);
  $("signin-email-form").hidden = !options.email;
  $("signin-none").hidden = Boolean(options.github || options.email);
  $("signin-key").open = !(options.github || options.email);
}

// Signing in by email is two steps in one card: ask for a code, then type
// it. The code is bound to this browser by a cookie the first step sets, so
// the second step has to happen here -- which is the point: the mail can
// open anywhere, the sign-in happens where you are.
async function askForCode(email) {
  const answer = await fetch("/auth/email", {method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({email, next: location.pathname + location.hash})});
  const body = await answer.json().catch(() => ({}));
  if (!answer.ok) throw new Error(body.error || answer.statusText);
}

$("signin-email-form")?.addEventListener("submit", async event => {
  event.preventDefault();
  const email = $("signin-email").value.trim();
  const button = event.target.querySelector("button");
  button.disabled = true;
  try {
    await askForCode(email);
    $("signin-code-to").textContent = email;
    $("signin-email-form").hidden = true;
    $("signin-code-form").hidden = false;
    $("signin-code").value = "";
    $("signin-code").focus();
    showSignInNote(`If ${email} can be mailed, a code is on its way.`);
  } catch (error) {
    showSignInNote(`The code could not be sent: ${error.message}`);
  } finally {
    button.disabled = false;
  }
});

$("signin-code-again")?.addEventListener("click", async () => {
  const email = $("signin-code-to").textContent;
  try { await askForCode(email); showSignInNote(`Another code is on its way to ${email}.`); }
  catch (error) { showSignInNote(`The code could not be sent: ${error.message}`); }
});

$("signin-code-form")?.addEventListener("submit", async event => {
  event.preventDefault();
  const email = $("signin-code-to").textContent;
  const code = $("signin-code").value.replace(/\D/g, "");
  const button = event.target.querySelector("button");
  button.disabled = true;
  try {
    const answer = await fetch("/auth/email/code", {method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({email, code})});
    const body = await answer.json().catch(() => ({}));
    if (!answer.ok) throw new Error(body.error || answer.statusText);
    location.href = body.next || "/app";
  } catch (error) {
    showSignInNote(error.message);
    $("signin-code").select();
  } finally {
    button.disabled = false;
  }
});

$("sign-out")?.addEventListener("click", async () => {
  try { await fetch("/auth/signout", {method: "POST", credentials: "same-origin"}); } catch {}
  token = "";
  sessionStorage.removeItem("farmToken");
  location.href = "/";
});

// Booted once every script the document names has run: the portal's document
// loads its shell after this file, and the boot asks the shell whether there
// are accounts to sign in. Kicked off here, it asked before the shell existed
// and a signed-in person was never signed in.
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => bootSession());
else bootSession();

let searchDebounce = null;
$("job-search").addEventListener("input", event => {
  clearTimeout(searchDebounce);
  const value = event.target.value;
  // Debounced: every keystroke is a query against the whole history, and an
  // operator types faster than SQLite should be asked to answer.
  searchDebounce = setTimeout(() => { runQuery.q = value; runQuery.offset = 0; loadJobs(); }, 250);
});
$("job-kind").addEventListener("change", event => { runQuery.kind = event.target.value; runQuery.offset = 0; loadJobs(); });
$("job-prev").addEventListener("click", () => { runQuery.offset = Math.max(0, runQuery.offset - runQuery.limit); loadJobs(); });
$("job-next").addEventListener("click", () => { runQuery.offset += runQuery.limit; loadJobs(); });

// Every clock on the page moves on its own: the poll is fifteen seconds
// apart when idle and three when busy, and a clock that only ticks with the
// poll looks broken while a suite runs for half an hour. Self-scheduling
// rather than a repeating timer, and stopped while the tab is hidden, so a
// backgrounded dashboard costs nothing — the same rule the poll follows.
function tickDurations() {
  clearTimeout(durationTimer);
  if (document.hidden) return;
  document.querySelectorAll("td.duration[data-started]").forEach(cell => {
    if (cell.dataset.final !== "" || !cell.dataset.started) return;
    cell.textContent = formatDuration((Date.now() - Date.parse(cell.dataset.started)) / 1000);
  });
  document.querySelectorAll("[data-timer-start]").forEach(node => {
    node.textContent = formatDuration((Date.now() - Date.parse(node.dataset.timerStart)) / 1000);
  });
  durationTimer = setTimeout(tickDurations, 1000);
}
tickDurations();
