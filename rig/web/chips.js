// What every page of the farm says a rig can do, in the same way.
//
// The dashboard (app.js) and the public site (site.js) are different pages
// -- one behind sign-in, one for anyone -- and both show a rig's capabilities
// as chips under its name. One renderer, loaded by both, so that a chip means
// the same thing wherever it is read. The words are the rig's own
// (rig_setup.py `label`): the count of boards, the access point's channel,
// the channels that are told.

function escapeHtml(value) {
  const node = document.createElement("span");
  node.textContent = String(value ?? "");
  return node.innerHTML;
}

// What a rig is set up to do. The states are the service's: `on` is set up
// *and* answering, `off` is a choice, `broken` is set up and not working, and
// `unknown` is a rig that has not said -- which is not the same as off.
const SETUP_STATE = {
  on: {tone: "good", label: "on"},
  off: {tone: "muted", label: "off"},
  broken: {tone: "bad", label: "not working"},
  unknown: {tone: "warn", label: "not reported"},
};

// A rig's capabilities as chips: what is on, and what is set up and not
// working. What is off is not shown -- a heading says what a rig can do, not
// what it cannot -- and the Setup tab keeps the whole table. A public row
// (rig_setup.public_headline) has no summary, and its chip has no title.
function capabilityChips(rows, {clickable = false} = {}) {
  return (rows || []).filter(row => ["on", "broken"].includes(row.state)).map(row => {
    const state = SETUP_STATE[row.state] || SETUP_STATE.unknown;
    const inner = `<span class="dot ${state.tone}"></span>${escapeHtml(row.label || row.title)}`;
    const title = row.summary ? ` title="${escapeHtml(`${row.title || row.label}: ${row.summary}`)}"` : "";
    return clickable
      ? `<button type="button" class="setup-chip ${state.tone} rig-capability" data-key="${escapeHtml(row.key)}"${title}>${inner}</button>`
      : `<span class="setup-chip ${state.tone}"${title}>${inner}</span>`;
  }).join("");
}
