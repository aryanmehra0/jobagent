/* Flow console client.
 *
 * Renders the six-phase pipeline as a node graph, streams run events over SSE,
 * and drives the same phases the CLI runs.
 *
 * Two sources of truth are kept deliberately separate:
 *   - `state`     : derived from artifacts on disk, refreshed from /api/state.
 *   - `runStatus` : transient per-phase status for the run in flight.
 * A node shows runStatus while a run is active and falls back to state otherwise,
 * so the graph is correct even if you reload mid-run or run a phase from the CLI.
 */

(() => {
  "use strict";

  const TOKEN = window.SESSION_TOKEN;

  /* ---------------- Graph layout ---------------- */

  // Positions are fixed per layout rather than auto-laid-out: the pipeline is a
  // known, unchanging shape, and a stable layout is easier to learn than a
  // solver's. Three hand-placed layouts exist because no single one suits every
  // window: a one-row flow is clearest but needs ~1570px, while the serpentine
  // (phases run left to right, drop a row, then return right to left) fits a
  // narrow window. fitLayout() picks whichever shows the pipeline largest and
  // scales it to fit, so all phases stay visible without scrolling.
  const COL = 232, ROW = 270, NODE_H = 240, TOP = 16, PHASE_Y = 150;
  const PHASE_IDS = ["intake", "source", "evaluate", "tailor", "apply", "track", "prep"];

  // Each layout maps node id -> [column, row]; the two input nodes sit one row
  // above the phase they feed (row 0), phases start at row 1 (y = PHASE_Y).
  const LAYOUTS = {
    row: {
      cells: { resume: [0, 0], settings: [1, 0], intake: [0, 1], source: [1, 1], evaluate: [2, 1],
               tailor: [3, 1], apply: [4, 1], track: [5, 1], prep: [6, 1] },
    },
    grid: {
      cells: { resume: [0, 0], settings: [1, 0], intake: [0, 1], source: [1, 1], evaluate: [2, 1],
               tailor: [3, 1], apply: [3, 2], track: [2, 2], prep: [1, 2] },
    },
    snake: {
      cells: { resume: [0, 0], settings: [1, 0], intake: [0, 1], source: [1, 1], evaluate: [2, 1],
               tailor: [2, 2], apply: [1, 2], track: [0, 2], prep: [0, 3] },
    },
  };
  const LAYOUT_ORDER = ["row", "grid", "snake"];

  function layoutSize(name) {
    const cells = Object.values(LAYOUTS[name].cells);
    const cols = Math.max(...cells.map((c) => c[0]));
    const rows = Math.max(...cells.map((c) => c[1]));
    return { w: cols * COL + 176, h: rowY(rows) + NODE_H };
  }
  function rowY(row) { return row === 0 ? TOP : PHASE_Y + (row - 1) * ROW; }

  let NODE_POS = {};
  let layoutName = "";
  let flowScale = 1;
  let fitMode = true;
  try { fitMode = localStorage.getItem("flowFit") !== "0"; } catch (_) { /* storage blocked */ }

  function applyLayout(name) {
    layoutName = name;
    NODE_POS = {};
    for (const [id, [c, r]] of Object.entries(LAYOUTS[name].cells)) {
      NODE_POS[id] = { x: c * COL, y: rowY(r), ...(r === 0 ? { kind: "input" } : {}) };
    }
  }

  /**
   * Pick the layout that renders largest in the canvas and scale it to fit.
   * In "actual size" mode nothing is scaled; the widest layout that fits the
   * width wins and the canvas scrolls vertically if it must.
   * Returns true when the layout changed, so the caller can rebuild the nodes.
   */
  function fitLayout() {
    const canvas = $(".canvas");
    const availW = Math.max(320, canvas.clientWidth - 56);
    // On narrow screens the page scrolls normally and the canvas is as tall as
    // its content, so height must not feed back into the scale (it would just
    // measure its own previous answer).
    const natural = window.matchMedia("(max-width: 1100px)").matches;
    const availH = natural ? Infinity : Math.max(320, canvas.clientHeight - 56);
    let best = "snake", bestScale = -1;
    if (fitMode) {
      // Width is never traded away (horizontal scrolling is the worst outcome),
      // but height may shrink only down to a readable floor; below that the
      // canvas scrolls vertically. The first layout that renders comfortably
      // large wins, so wide windows get the clearest one-row flow.
      const READABLE = 0.85, FLOOR = 0.8;
      for (const name of LAYOUT_ORDER) {
        const { w, h } = layoutSize(name);
        const byWidth = Math.min(1, availW / w);
        const scale = Math.max(Math.min(byWidth, availH / h), Math.min(byWidth, FLOOR));
        if (scale >= READABLE) { best = name; bestScale = scale; break; }
        if (scale > bestScale + 0.001) { best = name; bestScale = scale; }
      }
    } else {
      best = LAYOUT_ORDER.find((name) => layoutSize(name).w <= availW) || "snake";
      bestScale = 1;
    }
    flowScale = fitMode ? bestScale : 1;
    const { w, h } = layoutSize(best);
    const sizer = $("#flow-sizer"), flow = $("#flow");
    sizer.style.width = `${Math.round(w * flowScale)}px`;
    sizer.style.height = `${Math.round(h * flowScale)}px`;
    flow.style.width = `${w}px`;
    flow.style.height = `${h}px`;
    flow.style.transform = `scale(${flowScale})`;
    const fitBtn = $("#fit-btn");
    if (fitBtn) {
      fitBtn.textContent = fitMode ? "Fit to view ✓" : "Actual size";
      fitBtn.setAttribute("aria-pressed", String(fitMode));
    }
    const changed = best !== layoutName;
    if (changed) applyLayout(best);
    return changed;
  }

  // [from, to, label] — the label shows the count handed to the next phase.
  const EDGES = [
    ["resume", "intake", "PDF"],
    ["settings", "source", "search"],
    ["intake", "source", "profile"],
    ["source", "evaluate", "jobs"],
    ["evaluate", "tailor", "qualified"],
    ["tailor", "apply", "resumes"],
    ["apply", "track", "outcomes"],
    ["track", "prep", "practice"],
  ];

  const SVG_NS = "http://www.w3.org/2000/svg";

  // Edge endpoints are measured from the rendered nodes rather than assumed.
  // Node height varies with how many metric chips a phase has, and a fixed
  // height made connectors attach above or below the cards.
  function anchorRects() {
    const flow = $("#flow").getBoundingClientRect();
    const rects = {};
    // The flow is CSS-scaled to fit the window; bounding boxes come back scaled,
    // but the SVG lives in the unscaled coordinate space, so undo the scale.
    const k = flowScale || 1;
    document.querySelectorAll(".node[data-node]").forEach((node) => {
      const box = node.getBoundingClientRect();
      const left = (box.left - flow.left) / k, top = (box.top - flow.top) / k;
      const width = box.width / k, height = box.height / k;
      rects[node.dataset.node] = {
        left, right: left + width, top, bottom: top + height,
        cx: left + width / 2, cy: top + height / 2,
      };
    });
    return rects;
  }

  const GAP = 9;  // Clearance so an arrowhead never overlaps the target card.

  /**
   * Choose which sides of two cards an edge should join, and the control points
   * for the curve between them. Picking the nearest facing sides is what lets the
   * same routine draw the left-to-right row, the drop between rows, and the
   * right-to-left return row without any of them crossing a node.
   */
  function edgeGeometry(a, b) {
    if (b.left >= a.right - 20) {                       // target to the right
      const x1 = a.right, y1 = a.cy, x2 = b.left - GAP, y2 = b.cy;
      const mid = (x1 + x2) / 2;
      return { d: `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`,
               lx: mid, ly: (y1 + y2) / 2 - 9 };
    }
    if (b.right <= a.left + 20) {                       // target to the left
      const x1 = a.left, y1 = a.cy, x2 = b.right + GAP, y2 = b.cy;
      const mid = (x1 + x2) / 2;
      return { d: `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`,
               lx: mid, ly: (y1 + y2) / 2 - 9 };
    }
    if (b.top >= a.bottom - 20) {                       // target below
      const x1 = a.cx, y1 = a.bottom, x2 = b.cx, y2 = b.top - GAP;
      const mid = (y1 + y2) / 2;
      return { d: `M ${x1} ${y1} C ${x1} ${mid}, ${x2} ${mid}, ${x2} ${y2}`,
               lx: (x1 + x2) / 2 + 46, ly: mid + 4 };
    }
    const x1 = a.cx, y1 = a.top, x2 = b.cx, y2 = b.bottom + GAP;  // target above
    const mid = (y1 + y2) / 2;
    return { d: `M ${x1} ${y1} C ${x1} ${mid}, ${x2} ${mid}, ${x2} ${y2}`,
             lx: (x1 + x2) / 2 + 46, ly: mid + 4 };
  }

  /* ---------------- State ---------------- */

  let state = null;
  let running = false;
  let selected = "intake";
  let bannerDismissed = false;
  // Which phase the current run was started for, so the setup wizard can follow
  // an intake it kicked off without reacting to unrelated runs.
  let setupWatching = null;
  const runStatus = {};   // phase -> "running" | "ready" | "error" | "halted"
  const runSummary = {};  // phase -> summary from the last completed run
  const logLines = [];
  const logFilter = { query: "", level: "all" };
  let activePhaseId = null;   // the phase currently running, for the live elapsed timer
  let phaseStartedAt = null;

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };

  /* ---------------- Icons ---------------- */

  // Inline stroke icons (Lucide, ISC licence) so the console needs no icon font
  // or network request, and icons inherit text colour in both themes. The
  // strings are constants defined here, never user data.
  const ICONS = {
    briefcase: '<rect width="20" height="14" x="2" y="7" rx="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/>',
    "file-text": '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/>',
    sliders: '<line x1="4" x2="4" y1="21" y2="14"/><line x1="4" x2="4" y1="10" y2="3"/><line x1="12" x2="12" y1="21" y2="12"/><line x1="12" x2="12" y1="8" y2="3"/><line x1="20" x2="20" y1="21" y2="16"/><line x1="20" x2="20" y1="12" y2="3"/><line x1="2" x2="6" y1="14" y2="14"/><line x1="10" x2="14" y1="8" y2="8"/><line x1="18" x2="22" y1="16" y2="16"/>',
    intake: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/>',
    source: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    evaluate: '<path d="m3 17 2 2 4-4"/><path d="m3 7 2 2 4-4"/><path d="M13 6h8"/><path d="M13 12h8"/><path d="M13 18h8"/>',
    tailor: '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
    apply: '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
    track: '<line x1="12" x2="12" y1="20" y2="10"/><line x1="18" x2="18" y1="20" y2="4"/><line x1="6" x2="6" y1="20" y2="16"/>',
    prep: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    play: '<polygon points="6 3 20 12 6 21 6 3"/>',
    theme: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/>',
    copy: '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
    trash: '<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>',
    clock: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    chevron: '<path d="m6 9 6 6 6-6"/>',
    arrow: '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
  };

  function icon(name) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.innerHTML = ICONS[name] || "";
    return svg;
  }

  // Static markup marks where an icon goes with data-icon="name".
  function hydrateIcons(root = document) {
    root.querySelectorAll("[data-icon]").forEach((slot) => {
      if (!slot.querySelector("svg")) slot.prepend(icon(slot.dataset.icon));
    });
  }

  /* ---------------- API ---------------- */

  async function api(path, options = {}) {
    const res = await fetch(path, {
      ...options,
      headers: { "X-Session-Token": TOKEN, ...(options.headers || {}) },
    });
    const isJson = (res.headers.get("Content-Type") || "").includes("json");
    const payload = isJson ? await res.json() : null;
    if (!res.ok) throw new Error((payload && payload.error) || `HTTP ${res.status}`);
    return payload;
  }

  // A run can be started elsewhere: the CLI, a scheduled task, another window or
  // instance. This page only gets live events for runs its own server starts, so
  // it would sit on old data. Poll the state quietly (faster while a run is in
  // flight) and redraw only when something changed.
  let lastStateJson = "";
  async function syncState() {
    if (document.hidden) return;
    try {
      const data = await api("/api/state");
      const next = JSON.stringify(data);
      if (next === lastStateJson) return;
      lastStateJson = next;
      state = data.state;
      running = data.running;
      render();
    } catch { /* the next poll tries again; a toast every few seconds would only be noise */ }
  }

  function startStatePolling() {
    const tick = () => { syncState(); setTimeout(tick, running ? 6000 : 20000); };
    setTimeout(tick, 6000);
  }

  async function refreshState() {
    try {
      const data = await api("/api/state");
      state = data.state;
      running = data.running;
      render();
    } catch (err) {
      toast(`Could not load state: ${err.message}`, "err");
    }
  }

  /* ---------------- Toasts & modal ---------------- */

  function toast(message, kind = "") {
    const node = el("div", `toast ${kind}`, message);
    $("#toasts").append(node);
    setTimeout(() => node.remove(), 6000);
  }

  function confirmLive(count) {
    return new Promise((resolve) => {
      const root = $("#modal-root");
      root.innerHTML = "";

      const backdrop = el("div", "modal-backdrop");
      const modal = el("div", "modal");
      modal.append(el("h3", null, "Submit real job applications?"));

      const warn = el("div", "callout warn");
      warn.textContent =
        `This will open a browser and submit ${count} real application(s) under your name. ` +
        `It cannot be undone. Type APPLY to confirm.`;
      modal.append(warn);

      const input = el("input");
      input.type = "text";
      input.placeholder = "Type APPLY";
      input.style.cssText = "width:100%;padding:9px 11px;background:var(--bg);border:1px solid var(--border);border-radius:8px;";
      modal.append(input);

      const actions = el("div", "modal-actions");
      const cancel = el("button", "btn", "Cancel");
      const go = el("button", "btn btn-primary", "Submit applications");
      go.disabled = true;
      actions.append(cancel, go);
      modal.append(actions);

      input.addEventListener("input", () => { go.disabled = input.value.trim() !== "APPLY"; });
      const close = (value) => { root.innerHTML = ""; resolve(value); };
      cancel.addEventListener("click", () => close(false));
      go.addEventListener("click", () => close(true));
      backdrop.addEventListener("click", (e) => { if (e.target === backdrop) close(false); });
      document.addEventListener("keydown", function esc(e) {
        if (e.key === "Escape") { document.removeEventListener("keydown", esc); close(false); }
      });

      backdrop.append(modal);
      root.append(backdrop);
      input.focus();
    });
  }

  /**
   * A generic yes/no dialog.
   *
   * Resolves true for confirm and false for cancel, escape, or a backdrop click,
   * so the safe answer is always the one a stray keystroke produces.
   */
  function askConfirm({ title, body, confirmLabel = "Continue", cancelLabel = "Cancel", tone = "" }) {
    return new Promise((resolve) => {
      const root = $("#modal-root");
      root.innerHTML = "";

      const backdrop = el("div", "modal-backdrop");
      const modal = el("div", "modal");
      modal.append(el("h3", null, title));
      modal.append(el("div", `callout ${tone}`, body));

      const actions = el("div", "modal-actions");
      const cancel = el("button", "btn", cancelLabel);
      const confirm = el("button", "btn btn-primary", confirmLabel);
      actions.append(cancel, confirm);
      modal.append(actions);

      const close = (value) => { root.innerHTML = ""; document.removeEventListener("keydown", onKey); resolve(value); };
      function onKey(e) { if (e.key === "Escape") close(false); }

      cancel.addEventListener("click", () => close(false));
      confirm.addEventListener("click", () => close(true));
      backdrop.addEventListener("click", (e) => { if (e.target === backdrop) close(false); });
      document.addEventListener("keydown", onKey);

      backdrop.append(modal);
      root.append(backdrop);
      cancel.focus();
    });
  }

  /* ---------------- First-run setup wizard ---------------- */

  const setup = { open: false, step: 0, picked: null, parsing: false, log: [], result: null };

  function openSetup(step = 0) {
    setup.open = true;
    setup.step = step;
    setup.picked = setup.picked || currentResume();
    renderSetup();
  }

  function closeSetup() {
    setup.open = false;
    setup.parsing = false;
    setupWatching = null;
    $("#setup-root").innerHTML = "";
    render();
  }

  /**
   * Three steps: choose a resume, parse it and confirm what was extracted, then
   * check the search parameters. The wizard opens itself on first load when there
   * is no real profile, because every later phase is meaningless without one.
   */
  function renderSetup() {
    const root = $("#setup-root");
    root.innerHTML = "";
    if (!setup.open || !state) return;

    const backdrop = el("div", "setup-backdrop");
    const panel = el("div", "setup");

    const head = el("div", "setup-head");
    head.append(el("h2", null, "Set up your job agent"));
    head.append(el("p", null, "Everything the agent produces is built from your resume. Point it at yours to begin."));
    panel.append(head);

    const steps = el("div", "setup-steps");
    ["Resume", "Extracted profile", "Search"].forEach((_, index) => {
      const bar = el("div", `setup-step${index < setup.step ? " is-done" : index === setup.step ? " is-current" : ""}`);
      steps.append(bar);
    });
    panel.append(steps);

    const body = el("div", "setup-body");
    const foot = el("div", "setup-foot");

    if (setup.step === 0) renderSetupResume(body, foot);
    else if (setup.step === 1) renderSetupProfile(body, foot);
    else renderSetupSearch(body, foot);

    panel.append(body, foot);
    backdrop.append(panel);
    root.append(backdrop);
  }

  function renderSetupResume(body, foot) {
    body.append(el("h3", null, "1. Choose your resume"));
    body.append(el("p", null, "A PDF. The agent reads your roles, dates, skills and quantified achievements from it."));

    const drop = el("div", "dropzone",
      `Drop your resume here, or click to choose (${(state.formats || [".pdf"]).join(", ")})`);
    const picker = el("input");
    picker.type = "file"; picker.accept = acceptedFormats(); picker.hidden = true;
    drop.addEventListener("click", () => picker.click());
    picker.addEventListener("change", () => { if (picker.files[0]) uploadResume(picker.files[0], true); });
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("is-over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("is-over"));
    drop.addEventListener("drop", (e) => {
      e.preventDefault(); drop.classList.remove("is-over");
      if (e.dataTransfer.files[0]) uploadResume(e.dataTransfer.files[0], true);
    });
    body.append(drop, picker);

    if (state.resumes.length) {
      body.append(el("h4", null, "Already in data/raw_resumes/"));
      state.resumes.forEach((resume) => {
        const row = el("div", `resume-row${setup.picked === resume.name ? " is-selected" : ""}`);
        row.append(el("span", null, resume.is_sample ? "🧪" : "📄"));
        row.append(el("span", "grow", `${resume.name} — ${formatBytes(resume.size)}`));
        if (resume.is_sample) row.append(el("span", "tag-sample", "DEMO"));

        const remove = el("button", "remove", "✕");
        remove.title = `Remove ${resume.name}`;
        remove.addEventListener("click", async (e) => {
          e.stopPropagation();
          const ok = await askConfirm({
            title: `Delete ${resume.name}?`,
            tone: "err",
            body: `This permanently deletes the file from data/raw_resumes/. It does not change an already-parsed profile.`,
            confirmLabel: "Delete file",
          });
          if (ok) deleteResume(resume.name);
        });
        row.append(remove);

        row.addEventListener("click", () => { setup.picked = resume.name; renderSetup(); });
        body.append(row);
      });
    }

    if (!state.resumes.some((r) => !r.is_sample)) {
      body.append(el("div", "callout warn",
        "Only the bundled demo resume is present. Upload yours, or the agent will apply as its fictional candidate."));
    }

    const skip = el("button", "btn btn-ghost", "Skip for now");
    skip.addEventListener("click", closeSetup);
    foot.append(skip, el("div", "grow"));

    const next = el("button", "btn btn-primary", "Parse this resume →");
    next.disabled = !setup.picked;
    next.addEventListener("click", () => runIntakeFromSetup(setup.picked));
    foot.append(next);
  }

  function renderSetupProfile(body, foot) {
    body.append(el("h3", null, "2. Check what was extracted"));

    if (setup.parsing) {
      const status = el("p");
      status.append(el("span", "spinner-inline"), document.createTextNode(`Reading ${setup.picked}…`));
      body.append(status);
      body.append(el("div", "setup-log", setup.log.slice(-14).join("\n") || "Starting…"));
      foot.append(el("div", "grow"));
      const wait = el("button", "btn", "Working…");
      wait.disabled = true;
      foot.append(wait);
      return;
    }

    const intake = state.phases.intake;
    if (intake.status !== "ready") {
      body.append(el("div", "callout err",
        setup.result?.error || "The resume could not be parsed. Try another PDF, or check the Live log."));
      const back = el("button", "btn", "← Choose another");
      back.addEventListener("click", () => { setup.step = 0; renderSetup(); });
      foot.append(back, el("div", "grow"));
      const skip = el("button", "btn btn-ghost", "Close");
      skip.addEventListener("click", closeSetup);
      foot.append(skip);
      return;
    }

    body.append(el("p", null, "Confirm this is you. Everything downstream is locked to these facts."));

    const result = el("div", "parse-result");
    const dl = el("dl", "kv");
    dl.append(el("dt", null, "Name"), el("dd", null, intake.summary));
    Object.entries(intake.metrics || {}).forEach(([key, value]) => {
      dl.append(el("dt", null, key), el("dd", null, String(value)));
    });
    dl.append(el("dt", null, "From"), el("dd", null, intake.source_document || "—"));
    result.append(dl);
    body.append(result);

    if (intake.is_sample) {
      body.append(el("div", "callout warn",
        "This is still the bundled demo profile. Go back and upload your own resume."));
    }

    if (intake.gaps?.length) {
      const box = el("div", "callout warn");
      box.append(el("strong", null, "Worth filling in before you apply:"));
      const list = el("ul");
      intake.gaps.forEach((gap) => list.append(el("li", null, gap)));
      box.append(list);
      body.append(box);
    }

    const back = el("button", "btn", "← Wrong resume");
    back.addEventListener("click", () => { setup.step = 0; renderSetup(); });
    foot.append(back, el("div", "grow"));

    const next = el("button", "btn btn-primary", "That's me →");
    next.addEventListener("click", () => { setup.step = 2; renderSetup(); });
    foot.append(next);
  }

  function renderSetupSearch(body, foot) {
    body.append(el("h3", null, "3. What are you looking for?"));
    body.append(el("p", null, "These drive the job sweep. You can change them any time from the Settings tab."));

    const values = state.config.values || {};
    const form = el("form");
    form.id = "setup-config-form";
    form.append(textField("target_domains", "Target roles (comma separated)",
      (values.target_domains || []).join(", "), "For example: Backend Engineer, Platform Engineer"));
    form.append(textField("locations", "Locations (comma separated)",
      (values.locations || []).join(", "), "Use “Remote” for remote-first searches."));
    const row = el("div", "field-row");
    row.append(numberField("hours_old", "Posting age (hours)", values.hours_old ?? 48));
    row.append(numberField("min_salary", "Minimum salary", values.min_salary ?? "", true));
    form.append(row);
    body.append(form);

    const back = el("button", "btn", "← Back");
    back.addEventListener("click", () => { setup.step = 1; renderSetup(); });
    foot.append(back, el("div", "grow"));

    const finish = el("button", "btn btn-primary", "Finish setup");
    finish.addEventListener("click", async () => {
      const data = new FormData(form);
      const split = (key) => String(data.get(key) || "").split(",").map((s) => s.trim()).filter(Boolean);
      const salary = String(data.get("min_salary") || "").trim();
      const payload = {
        ...(state.config.values || {}),
        target_domains: split("target_domains"),
        locations: split("locations"),
        hours_old: Number(data.get("hours_old")) || 48,
        min_salary: salary === "" ? null : Number(salary),
        job_boards: values.job_boards || ["linkedin", "indeed"],
      };
      try {
        const saved = await api("/api/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        state = saved.state;
        toast("Setup complete. Press “Run all phases” when you are ready.", "ok");
        closeSetup();
      } catch (err) {
        toast(err.message, "err");
      }
    });
    foot.append(finish);
  }

  /** Upload the picked resume, then parse it, keeping the wizard in step. */
  async function runIntakeFromSetup(name) {
    setup.picked = name;
    setup.step = 1;
    setup.parsing = true;
    setup.log = [];
    setup.result = null;
    setupWatching = "intake";
    renderSetup();

    try {
      await api("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phases: ["intake"], options: { resume: name, dry_run: true } }),
      });
    } catch (err) {
      setup.parsing = false;
      setup.result = { error: err.message };
      setupWatching = null;
      renderSetup();
    }
  }

  async function deleteResume(name) {
    try {
      const result = await api("/api/resume/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      state = result.state;
      if (setup.picked === name) setup.picked = currentResume();
      toast(`Removed ${result.removed}.`, "ok");
      render();
      if (setup.open) renderSetup();
      if ($("#panel-settings").hidden === false) renderSettings();
    } catch (err) {
      toast(err.message, "err");
    }
  }

  /** The `accept` attribute for a file input, from the server's format list. */
  function acceptedFormats() {
    return (state?.formats || [".pdf"]).join(",");
  }

  /**
   * Diagnose a resume before building a profile from it.
   *
   * Returns the report, or null if the check itself failed. A blocker means the
   * document cannot be parsed at all, so the wizard shows the fixes rather than
   * letting intake fail with a bare error.
   */
  async function checkResume(name) {
    try {
      const result = await api("/api/resume/check", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      return result.report;
    } catch (err) {
      toast(err.message, "err");
      return null;
    }
  }

  /* ---------------- Run control ---------------- */

  async function startRun(phases, jobId = null) {
    if (running) { toast("A run is already in progress.", "err"); return; }

    // Guard against running as somebody else. A run that includes intake will
    // build the profile itself, so it only needs a resume to read; a run that
    // skips intake depends on whatever profile is already on disk.
    if (state?.setup) {
      const blocked = phases.includes("intake")
        ? await checkResumeBeforeRun()
        : await checkProfileBeforeRun();
      if (blocked) return;
    }

    const dryRun = $("#dry-run").checked;
    if (phases.includes("apply") && !dryRun) {
      const pending = (state?.phases?.tailor?.metrics?.["PDFs"]) || 0;
      if (!(await confirmLive(pending))) return;
    }

    for (const phase of phases) { runStatus[phase] = "pending"; }
    render();

    try {
      await api("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          phases,
          confirm_live: dryRun ? undefined : "APPLY",
          options: {
            resume: currentResume(),
            dry_run: dryRun,
            track_all: true,
            tailoring_mode: $("#resume-mode").value,
            cover_letter: $("#cover-letter").checked,
            tailor_all: runOptions.tailorAll,
            score_limit: runOptions.scoreLimit || undefined,
            job_id: jobId,
          },
        }),
      });
      switchTab("log");
    } catch (err) {
      for (const phase of phases) { delete runStatus[phase]; }
      render();
      toast(err.message, "err");
    }
  }

  function currentResume() {
    const picker = $("#resume-pick");
    if (picker && picker.value) return picker.value;
    // `resumes` is ordered with real resumes ahead of the bundled sample.
    return state?.resumes?.[0]?.name || null;
  }

  /**
   * Check the resume a run is about to parse.
   *
   * @returns {Promise<boolean>} true when the run should be abandoned.
   */
  async function checkResumeBeforeRun() {
    if (!state.resumes?.length) {
      toast("No resume available. Upload one to begin.", "err");
      openSetup(0);
      return true;
    }
    const picked = currentResume();
    const isSample = state.resumes.find((r) => r.name === picked)?.is_sample;
    if (!isSample) return false;

    const proceed = await confirmSampleRun(
      `"${picked}" is the bundled demo resume, not yours. Parsing it will build a ` +
      `profile for a fictional candidate, and every resume and email after that ` +
      `will carry those details.`
    );
    if (!proceed) openSetup(0);
    return !proceed;
  }

  /**
   * Check the profile a run will rely on.
   *
   * @returns {Promise<boolean>} true when the run should be abandoned.
   */
  async function checkProfileBeforeRun() {
    if (state.setup.needs_profile) {
      toast("No profile yet. Upload your resume and parse it first.", "err");
      openSetup(0);
      return true;
    }
    if (!state.setup.using_sample) return false;

    const name = state.phases.intake.summary;
    const proceed = await confirmSampleRun(
      `The loaded profile is "${name}", parsed from the bundled sample resume — not ` +
      `you. Resumes and outreach emails produced from it will carry those details.`
    );
    if (!proceed) openSetup(0);
    return !proceed;
  }

  /** Ask before letting the pipeline run against the bundled demo candidate. */
  function confirmSampleRun(body) {
    return askConfirm({
      title: "This is demo data, not you",
      tone: "warn",
      body,
      confirmLabel: "Continue with demo data",
      cancelLabel: "Use my resume instead",
    });
  }

  /* ---------------- Event stream ---------------- */

  // The server replays its last run's events to every new connection (to fill the
  // Live log). They describe the PAST, and a phase that failed in that run may
  // since have succeeded in another process or window, so they must not set a
  // phase's status: the files on disk are the truth. Each (re)connection starts a
  // short replay window during which statuses are not taken from events.
  let replaying = true;
  let lastEventAt = Date.now();

  function connectEvents() {
    const source = new EventSource("/api/events");
    let replayTimer = setTimeout(() => { replaying = false; }, 2000);
    source.onopen = () => {
      replaying = true;
      clearTimeout(replayTimer);
      replayTimer = setTimeout(() => { replaying = false; }, 1500);
    };

    source.onmessage = (event) => {
      let payload;
      try { payload = JSON.parse(event.data); } catch { return; }
      handleEvent(payload);
    };

    // EventSource reconnects on its own; re-syncing state on reconnect keeps the
    // graph correct if the server restarted while we were away.
    source.onerror = () => { setTimeout(refreshState, 2000); };
  }

  function handleEvent(evt) {
    if (!replaying) lastEventAt = Date.now();
    switch (evt.type) {
      case "run_start":
        ranThisSession = true;
        running = true;
        logLines.length = 0;
        pushLog("evt", `Run started: ${evt.phases.join(" → ")}`);
        render();
        break;

      case "phase_start":
        if (!replaying) { runStatus[evt.phase] = "running"; delete runSummary[evt.phase]; }
        activePhaseId = evt.phase;
        phaseStartedAt = Date.now();
        pushLog("evt", `▶ ${title(evt.phase)}`);
        render();
        break;

      case "log":
        if (evt.line) pushLog("", evt.line, evt.phase);
        if (setup.parsing && evt.line) {
          setup.log.push(evt.line);
          renderSetup();
        }
        break;

      case "phase_end": {
        const ok = ["ok", "warning"].includes(evt.status);
        if (!replaying) {
          runStatus[evt.phase] = evt.status === "warning" ? "warning" : ok ? "ready" : evt.status === "cancelled" ? "halted" : "error";
          runSummary[evt.phase] = evt.summary || {};
          if (evt.summary && evt.summary.halt_reason && evt.status !== "error") runStatus[evt.phase] = "halted";
        }

        if (setupWatching === evt.phase) {
          setup.parsing = false;
          setup.result = ok ? evt.summary : { error: evt.error };
          setupWatching = null;
          // The state snapshot arrives with run_end; the wizard redraws then.
        }
        pushLog(
          ok ? "evt" : "err",
          ok ? `✔ ${title(evt.phase)} finished in ${evt.duration}s`
             : `✖ ${title(evt.phase)} ${evt.status}${evt.error ? `: ${evt.error}` : ""}`
        );
        if (activePhaseId === evt.phase) { activePhaseId = null; phaseStartedAt = null; }
        render();
        break;
      }

      case "state":
        state = evt.snapshot;
        render();
        if (setup.open) renderSetup();
        break;

      case "run_end":
        running = false;
        activePhaseId = null;
        phaseStartedAt = null;
        pushLog("evt", `Run ${evt.status}.`);
        // The server replays the previous run's events when the page connects;
        // announcing those would pop a stale "Run warning." on every reload.
        if (!replaying) {
          toast(
            evt.status === "ok" ? "Run finished."
              : evt.status === "warning" ? "Run finished with warnings. Check the Live log."
              : `Run ${evt.status}.`,
            evt.status === "ok" ? "ok" : evt.status === "warning" ? "warn" : "err"
          );
        }
        refreshState();
        if ($("#jobs-dialog").open) openJobs();
        break;
    }
  }

  // Patterns the backend's plain-text lines use for warnings/errors that don't
  // arrive with an explicit "err" kind (e.g. a Groq cooldown, a PDF check).
  const WARN_PATTERN = /\b(warning|retry|retrying|cooldown|rate limit|429|skipped|fallback|manual apply needed)\b/i;
  const ERR_PATTERN = /\b(error|failed|✖|exception|traceback|access is denied)\b/i;
  const OK_PATTERN = /(✔|\bpass\b|\bqualified\b|\bok\b)/i;

  function logLevel(entry) {
    if (entry.kind === "err") return "err";
    if (entry.kind === "evt") return "evt";
    if (ERR_PATTERN.test(entry.line)) return "err";
    if (WARN_PATTERN.test(entry.line)) return "warn";
    if (OK_PATTERN.test(entry.line)) return "ok";
    return "";
  }

  function formatElapsed(ms) {
    const s = Math.max(0, ms) / 1000;
    return s < 60 ? `${s.toFixed(0)}s` : `${Math.floor(s / 60)}m ${Math.floor(s % 60)}s`;
  }

  // Updates only the running node's elapsed-time text, not a full render(), so
  // a once-a-second tick doesn't disturb scroll position, focus or selection
  // anywhere else in the UI.
  function tickRunningTimer() {
    if (!running || !activePhaseId || !phaseStartedAt) return;
    const node = document.querySelector(`.node[data-node="${activePhaseId}"] .node-elapsed`);
    if (node) node.textContent = formatElapsed(Date.now() - phaseStartedAt);
  }

  // Gap since the previous line, captured at push time (not render time) so
  // filtering/searching the log never distorts how long a step actually took.
  function pushLog(kind, line, phase) {
    const ts = Date.now();
    const prev = logLines[logLines.length - 1];
    const gapMs = prev && kind !== "evt" && prev.kind !== "evt" ? ts - prev.ts : 0;
    logLines.push({ kind, line, phase, ts, gapMs });
    if (logLines.length > 1200) logLines.splice(0, logLines.length - 1200);
    renderLog();
  }

  /* ---------------- Rendering ---------------- */

  const title = (id) => state?.phases?.[id]?.title || id;

  function nodeStatus(id) {
    const report = state?.run_report;
    if (running && report?.active_phase === id) return "running";
    if (report?.status === "interrupted" && report.active_phase === id) return "halted";
    if (!running && ["error", "warning"].includes(report?.phases?.[id]?.status)) return report.phases[id].status;
    if (runStatus[id] === "pending") return "empty";
    if (runStatus[id]) return runStatus[id];
    return state?.phases?.[id]?.status || "empty";
  }

  const STATUS_LABEL = {
    empty: "Not run",
    ready: "Complete",
    running: "Running",
    error: "Failed",
    halted: "Stopped",
    warning: "Needs review",
  };

  function renderBanner() {
    const banner = $("#sample-banner");
    const info = state?.setup;
    if (!info || bannerDismissed || (!info.using_sample && !info.needs_profile)) {
      banner.hidden = true;
      return;
    }
    banner.hidden = false;
    $("#sample-banner-text").textContent = info.needs_profile
      ? "No profile yet. Upload your resume so the agent works from your real details."
      : `Running on demo data: the loaded profile is "${state.phases.intake.summary}", `
        + "parsed from the bundled sample resume. Upload your own resume before applying.";
  }

  let runBannerDismissed = false;
  let ranThisSession = false;

  function renderRunBanner() {
    const banner = $("#run-banner");
    const last = state?.last_run;
    if (!last || running || ranThisSession || runBannerDismissed) {
      banner.hidden = true;
      return;
    }
    const when = new Date(last.at);
    const age = last.age_hours < 1 ? "less than an hour ago"
      : last.age_hours < 48 ? `${Math.round(last.age_hours)} hours ago`
      : `${Math.round(last.age_hours / 24)} days ago`;
    banner.hidden = false;
    const full =
      `These are results from your previous run (${when.toLocaleString()}, ${age}), not a new search. ` +
      "Press Run all phases to refresh the search. Previously seen listings can appear in the latest CSV; processed applications are not repeated. " +
      "Start fresh clears this view first.";
    // One line in the banner; the full explanation stays available on hover and
    // to screen readers instead of eating three lines of canvas.
    const text = $("#run-banner-text");
    text.textContent = `Showing your previous run — ${when.toLocaleString()} (${age}), not a new search. Press Run all phases to refresh.`;
    text.title = full;
  }

  function render() {
    renderBanner();
    renderRunBanner();
    renderPills();
    renderNextStep();
    renderNodes();
    // Deferred a frame: edge anchors are measured from the DOM, which only has
    // real geometry once the browser has laid the new nodes out.
    requestAnimationFrame(renderEdges);
    renderPanel();
    // With the log on its own tab, a pulsing dot says a run is producing output.
    document.querySelector('.tab[data-tab="log"]')?.classList.toggle("is-live", running);
    $("#run-btn").disabled = running;
    $("#cancel-btn").disabled = !running;
    $("#run-btn").replaceChildren(icon("play"), el("span", null, running ? "Running…" : "Run all phases"));
    $("#run-btn").setAttribute("aria-label", running ? "Running, please wait" : "Run all phases");

    const dry = $("#dry-run").checked;
    $("#dry-toggle").classList.toggle("is-live", !dry);
    $("#dry-label").textContent = dry ? "Dry run" : "LIVE — submits real applications";
    $("#dry-toggle").title = dry
      ? "Dry run is on: nothing is submitted. Untick to submit real applications."
      : "Live mode: auto-apply will submit real applications.";
  }

  /**
   * Work out the single most useful thing to do now, from the same state the
   * nodes show, so the card can never disagree with the graph. Order matters:
   * a missing profile blocks everything, then each phase in pipeline order, and
   * only when the pipeline is healthy does it point at the human steps.
   */
  function nextStep() {
    if (!state) return null;
    const metric = (id, key) => Number(state.phases[id]?.metrics?.[key]) || 0;
    const status = (id) => nodeStatus(id);

    if (running) {
      const phase = activePhaseId || state.run_report?.active_phase;
      const label = phase && state.phases[phase]?.title;
      // No events for a while but a run is in flight: it was started somewhere else.
      const elsewhere = !activePhaseId && Date.now() - lastEventAt > 15000;
      return {
        title: label ? `Running ${label}…` : "Running…",
        detail: elsewhere
          ? "A run started elsewhere (the command line, a scheduled task or another window) is in progress. This page follows it automatically."
          : "Watch progress in the Live log. Stop is available in the top bar.",
      };
    }
    if (state.setup && (state.setup.needs_profile || state.setup.using_sample)) {
      return { title: "Upload your resume", detail: "Every phase needs your real profile first.", action: "Set up resume", run: () => openSetup(0) };
    }
    if (status("intake") !== "ready") {
      return { title: "Parse your resume", detail: "Phase 1 builds and seals your profile.", action: "Run resume intake", run: () => startRun(["intake"]) };
    }
    // Country, visa needs and salary are not on a resume, and a new candidate does not
    // inherit the previous one's. Ask before the first search, where they shape which
    // jobs are eligible and how they are scored.
    const saved = state.preferences?.values || {};
    const hasPreferences = saved.current_country || (saved.authorized_countries || []).length;
    if (!hasPreferences && status("source") === "empty") {
      return { title: "Add your location and work preferences",
               detail: "Eligibility and scoring depend on your country, visa needs and salary range, and they are not on a resume.",
               action: "Open preferences",
               run: () => { settingsOpen.add("Candidate preferences"); switchTab("settings"); } };
    }
    if (status("source") === "empty") {
      return { title: "Find jobs", detail: "Search your configured boards for matching roles.", action: "Run sourcing", run: () => startRun(["source"]) };
    }
    const missing = metric("evaluate", "Missing description");
    const deferred = metric("evaluate", "Deferred by limit");
    const qualified = metric("evaluate", "Qualified");
    if (status("evaluate") === "empty") {
      return { title: "Score the jobs", detail: "Rank sourced jobs against your profile.", action: "Run evaluation", run: () => startRun(["evaluate"]) };
    }
    if (status("tailor") === "empty" && qualified > 0) {
      return { title: "Tailor your resume", detail: `${qualified} qualified job(s) are ready for tailored PDFs.`, action: "Run tailoring", run: () => startRun(["tailor"]) };
    }
    const manual = metric("apply", "Manual apply needed");
    // What is left over never outranks work the candidate can act on today.
    const leftovers = [
      deferred > 0 ? `${deferred} more job${deferred === 1 ? " is" : "s are"} waiting to be scored.` : "",
      missing > 0 ? `${missing} could not be scored because ${missing === 1 ? "it" : "they"} had no readable description.` : "",
    ].filter(Boolean).join(" ");
    if (manual > 0) {
      return { title: `${manual} application${manual === 1 ? "" : "s"} need you to apply`,
               detail: `Open each job, attach the tailored PDF and send it yourself. ${leftovers}`.trim(), action: "Open shortlist", run: openJobs };
    }
    if (qualified > 0) {
      return { title: `${qualified} job${qualified === 1 ? " is" : "s are"} ready for your review`,
               detail: `Each has a tailored resume and an explanation of its score. ${leftovers}`.trim(), action: "Open shortlist", run: openJobs };
    }
    if (deferred > 0) {
      return { title: `${deferred} job${deferred === 1 ? "" : "s"} still to score`,
               detail: "Evaluation stopped at its limit before reaching them. Run it again to continue.",
               action: "Score the rest", run: () => startRun(["evaluate"]) };
    }
    if (missing > 0) {
      return { title: `${missing} job${missing === 1 ? "" : "s"} could not be scored`,
               detail: "They had no readable description. Re-running evaluation retries them.",
               action: "Retry evaluation", run: () => startRun(["evaluate"]) };
    }
    return { title: "Pipeline is up to date", detail: "Review your shortlist, or run all phases to refresh the search.", action: "Open shortlist", run: openJobs };
  }

  function renderNextStep() {
    const card = $("#next-step");
    const step = nextStep();
    card.hidden = !step;
    if (!step) return;
    $("#next-step-title").textContent = step.title;
    $("#next-step-detail").textContent = step.detail;
    const button = $("#next-step-action");
    button.hidden = !step.action;
    button.textContent = step.action || "";
    button.onclick = step.run || null;
  }

  function renderPills() {
    if (!state) return;
    const provider = $("#pill-provider");
    const hasLLM = state.provider && state.provider !== "none";
    provider.className = `pill ${hasLLM ? "ok" : "warn"}`;
    provider.lastElementChild.textContent = hasLLM
      ? `LLM: ${state.provider}`
      : "LLM: none (deterministic fallbacks)";

    const profile = $("#pill-profile");
    const intake = state.phases.intake;
    const sealed = intake.status === "ready";
    profile.className = `pill ${sealed ? "ok" : "warn"}`;
    profile.lastElementChild.textContent = sealed
      ? `Profile: ${intake.summary}`
      : "Profile: not ready";
  }

  function renderNodes() {
    const flow = $("#flow");
    // Nodes are rebuilt on every render; keep keyboard focus on the same card.
    const focusedNode = document.activeElement?.closest?.(".node")?.dataset.node;
    flow.querySelectorAll(".node").forEach((n) => n.remove());
    if (!state) return;
    fitLayout();
    if (focusedNode) requestAnimationFrame(() => {
      const card = flow.querySelector(`.node[data-node="${focusedNode}"]`);
      (card?.querySelector(".node-body") || card)?.focus({ preventScroll: true });
    });

    // Input nodes describe what you feed the pipeline.
    flow.append(inputNode("resume", "Resume PDF", resumeLabel(), "file-text"));
    flow.append(inputNode("settings", "Search Settings", settingsLabel(), "sliders"));

    state.order.forEach((id, index) => {
      flow.append(phaseNode(id, index + 1));
    });
  }

  function resumeLabel() {
    const count = state?.resumes?.length || 0;
    if (!count) return "No resume uploaded";
    return currentResume() || `${count} available`;
  }

  function settingsLabel() {
    const cfg = state?.config;
    if (!cfg || cfg.status !== "ready") return "Not configured";
    return `${cfg.values.target_domains.length} role(s), ${cfg.values.job_boards.length} board(s)`;
  }

  // Nodes contain their own "Run" button, and a <button> may not nest another
  // button, so the card is a focusable div that behaves like one.
  function makeActivatable(node, label) {
    node.tabIndex = 0;
    node.setAttribute("role", "button");
    node.setAttribute("aria-label", label);
    node.addEventListener("keydown", (e) => {
      if (e.target !== node || (e.key !== "Enter" && e.key !== " ")) return;
      e.preventDefault();
      node.click();
    });
  }

  function inputNode(id, label, sub, iconName) {
    const pos = NODE_POS[id];
    const node = el("div", `node node-input status-${state?.config?.status === "error" && id === "settings" ? "error" : "ready"}`);
    node.style.left = `${pos.x}px`;
    node.style.top = `${pos.y}px`;
    node.dataset.node = id;
    makeActivatable(node, `${label}: ${sub}`);

    const head = el("div", "node-head");
    const tile = el("div", "node-icon"); tile.append(icon(iconName));
    head.append(tile, el("div", "node-title", label));
    node.append(head, el("div", "node-sub", sub));

    node.addEventListener("click", () => { selected = id; switchTab("settings"); render(); });
    return node;
  }

  // A phase card holds two separate controls: a "select" button (the title,
  // status and summary) and the Run button. They are siblings because a button
  // may not contain another button, and assistive tech hides the children of
  // anything with role="button", so a Run button inside the card was unreachable
  // by name. Mouse users can still click anywhere on the card to select it.
  function phaseNode(id, index) {
    const phase = state.phases[id];
    const status = nodeStatus(id);
    const pos = NODE_POS[id];

    const node = el("div", `node status-${status}${selected === id ? " is-selected" : ""}`);
    node.style.left = `${pos.x}px`;
    node.style.top = `${pos.y}px`;
    node.dataset.node = id;
    node.setAttribute("role", "group");
    node.setAttribute("aria-label", `Phase ${index}: ${phase.title}`);

    const select = el("button", "node-body");
    select.type = "button";
    select.setAttribute("aria-pressed", String(selected === id));
    select.setAttribute("aria-label", `Phase ${index}, ${phase.title}: ${STATUS_LABEL[status] || status}. ${phase.summary}. Show details.`);

    const head = el("span", "node-head");
    const tile = el("span", "node-icon"); tile.append(icon(id));
    head.append(tile, el("span", "node-title", phase.title), el("span", "node-index", String(index)));
    select.append(head, el("span", "node-sub", phase.subtitle));

    const badge = el("span", "node-status");
    if (status === "running") badge.append(el("span", "spin"));
    badge.append(el("span", null, STATUS_LABEL[status] || status));
    if (status === "running" && activePhaseId === id) {
      badge.append(el("span", "node-elapsed", formatElapsed(Date.now() - phaseStartedAt)));
    }
    select.append(badge, el("span", "node-summary", phase.summary));
    node.append(select);

    // Label/value rows rather than boxed chips: three numbers read as a column
    // you can scan, where chips read as decoration.
    const metrics = el("dl", "node-metrics");
    Object.entries(phase.metrics || {}).slice(0, 3).forEach(([key, value]) => {
      const row = el("div", "metric");
      row.append(el("dt", null, key), el("dd", null, String(value)));
      metrics.append(row);
    });
    if (metrics.children.length) node.append(metrics);

    const run = el("button", "btn btn-sm node-run");
    run.type = "button";
    run.setAttribute("aria-label", `Run ${phase.title.toLowerCase()}`);
    run.append(icon("play"), el("span", null, `Run ${phase.title.toLowerCase()}`));
    run.disabled = running;
    run.addEventListener("click", (e) => { e.stopPropagation(); startRun([id]); });
    node.append(run);

    node.addEventListener("click", () => { selected = id; switchTab("details"); render(); });
    return node;
  }

  function renderEdges() {
    const svg = $("#edges");
    svg.innerHTML = "";
    if (!state) return;

    // Arrowheads, one per edge state, so the direction of flow is unambiguous.
    const defs = document.createElementNS(SVG_NS, "defs");
    const markers = [["arrow", ""], ["arrow-active", " is-active"], ["arrow-flow", " is-flowing"]];
    for (const [id, variant] of markers) {
      const marker = document.createElementNS(SVG_NS, "marker");
      marker.setAttribute("id", id);
      marker.setAttribute("viewBox", "0 0 8 8");
      marker.setAttribute("refX", "7");
      marker.setAttribute("refY", "4");
      marker.setAttribute("markerWidth", "7");
      marker.setAttribute("markerHeight", "7");
      marker.setAttribute("orient", "auto-start-reverse");
      const head = document.createElementNS(SVG_NS, "path");
      head.setAttribute("d", "M 0 1 L 7 4 L 0 7 z");
      head.setAttribute("class", `arrowhead${variant}`);
      marker.append(head);
      defs.append(marker);
    }
    svg.append(defs);

    const rects = anchorRects();

    for (const [from, to, label] of EDGES) {
      const a = rects[from], b = rects[to];
      if (!a || !b) continue;

      const geometry = edgeGeometry(a, b);

      // Input nodes are always satisfied; a phase edge lights up once its
      // upstream phase has produced output.
      const fromDone = NODE_POS[from].kind === "input" || nodeStatus(from) === "ready";
      const toRunning = nodeStatus(to) === "running";
      const variant = toRunning ? " is-flowing" : fromDone ? " is-active" : "";
      const marker = toRunning ? "arrow-flow" : fromDone ? "arrow-active" : "arrow";

      const path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", geometry.d);
      path.setAttribute("class", `edge${variant}`);
      path.setAttribute("marker-end", `url(#${marker})`);
      svg.append(path);

      const count = edgeCount(from, to);
      if (count === null) continue;

      const text = document.createElementNS(SVG_NS, "text");
      text.setAttribute("x", String(geometry.lx));
      text.setAttribute("y", String(geometry.ly));
      text.setAttribute("class", `edge-label${fromDone ? " is-active" : ""}`);
      text.textContent = `${count} ${label}`;
      svg.append(text);
    }
  }

  // The number handed from one phase to the next, which is what makes the graph
  // readable at a glance: you can see where the funnel narrows.
  function edgeCount(from, to) {
    if (!state) return null;
    const m = (id, key) => state.phases[id]?.metrics?.[key];
    switch (`${from}>${to}`) {
      case "source>evaluate":  return m("source", "This sweep") ?? null;
      case "evaluate>tailor":  return m("evaluate", "Qualified") ?? null;
      case "tailor>apply":     return m("tailor", "PDFs") ?? null;
      case "apply>track":      return (m("apply", "Submitted") ?? 0) + (m("apply", "Fallbacks") ?? 0);
      default: return null;
    }
  }

  /* ---------------- Inspector ---------------- */

  function switchTab(name) {
    document.querySelectorAll(".tab").forEach((tab) => {
      const active = tab.dataset.tab === name;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
    });
    $("#panel-details").hidden = name !== "details";
    $("#panel-log").hidden = name !== "log";
    $("#panel-settings").hidden = name !== "settings";
    $("#panel-analytics").hidden = name !== "analytics";
    $("#panel-history").hidden = name !== "history";
    if (name === "analytics") renderAnalytics();
    if (name === "settings") renderSettings();
    if (name === "history") renderHistory();
  }

  // A labelled horizontal bar: the value is always printed next to the bar, so
  // the chart is never the only way to read a number.
  function barRow(label, value, sub, fraction, tone) {
    const row = el("div", "bar-row");
    const head = el("div", "bar-head");
    head.append(el("span", "bar-label", label), el("span", "bar-value", value));
    const track = el("div", "bar-track");
    track.setAttribute("role", "img");
    track.setAttribute("aria-label", `${label}: ${value}`);
    const fill = el("div", `bar-fill${tone ? ` ${tone}` : ""}`);
    fill.style.width = `${Math.max(0, Math.min(1, fraction || 0)) * 100}%`;
    track.append(fill);
    row.append(head, track);
    if (sub) row.append(el("div", "bar-sub", sub));
    return row;
  }

  function statTile(label, value, hint) {
    const tile = el("div", "stat-tile");
    tile.append(el("div", "stat-value", value), el("div", "stat-label", label));
    if (hint) tile.append(el("div", "stat-hint", hint));
    return tile;
  }

  async function renderAnalytics() {
    const panel = $("#panel-analytics");
    panel.replaceChildren(el("p", "hint", "Loading outcomes…"));
    try {
      const [result, perf] = await Promise.all([
        api("/api/analytics"),
        api("/api/performance").catch(() => null),
      ]);
      panel.replaceChildren(el("h3", null, "Application outcomes"));
      if (result.note) panel.append(el("p", "hint", result.note));
      const lastRun = lastRunCard();
      if (lastRun) { panel.append(el("h4", null, "Last run"), lastRun); panel.append(el("h4", null, "Outcomes")); }

      const funnel = result.funnel || [];
      const top = Math.max(1, funnel[0]?.count || 0);
      if (!funnel.length || !funnel[0].count) {
        const empty = el("div", "empty-state");
        empty.append(el("strong", null, "No applications recorded yet"),
          el("span", null, "Run the pipeline and mark jobs as applied; outcomes and reply rates will appear here."));
        panel.append(empty);
      }

      // Headline numbers first: what happened, and how fast replies come.
      const last = funnel[funnel.length - 1];
      const tiles = el("div", "stat-tiles");
      if (funnel[0]) tiles.append(statTile(funnel[0].stage, String(funnel[0].count)));
      if (last && last !== funnel[0]) tiles.append(statTile(last.stage, String(last.count)));
      tiles.append(statTile("Median reply",
        result.median_response_days == null ? "—" : `${result.median_response_days.toFixed(1)}d`,
        result.median_response_days == null ? "No dated replies yet" : `${result.timed_responses} dated repl${result.timed_responses === 1 ? "y" : "ies"}`));
      panel.append(tiles);

      panel.append(el("h4", null, "Funnel"));
      const funnelBox = el("div", "bars");
      funnel.forEach((stage, i) => {
        funnelBox.append(barRow(stage.stage, String(stage.count),
          stage.conversion == null ? (i ? "Conversion unknown" : "") : `${(stage.conversion * 100).toFixed(1)}% of previous stage`,
          stage.count / top));
      });
      panel.append(funnelBox);

      for (const [key, heading] of [["by_score", "Reply rate by fit score"], ["by_source", "By source"], ["by_variant", "By resume format"], ["by_role", "By role"]]) {
        const rows = result[key] || [];
        if (!rows.length) continue;
        panel.append(el("h4", null, heading));
        const box = el("div", "bars");
        for (const row of rows) {
          const rate = result.reply_tracking ? `${(row.response_rate * 100).toFixed(1)}%` : "n/a";
          box.append(barRow(row.label, `${row.replied}/${row.applied} replied`,
            result.reply_tracking ? rate : "Reply tracking unavailable",
            result.reply_tracking ? row.response_rate : 0, "is-ok"));
        }
        panel.append(box);
      }
      renderLatency(panel, perf);
    } catch (error) {
      const failed = el("div", "callout err", `Could not load analytics: ${error.message}`);
      panel.replaceChildren(failed);
    }
  }

  function renderLatency(panel, perf) {
    panel.append(el("h4", null, "Phase latency"));
    if (!perf || !perf.phase_stats.length) {
      panel.append(el("p", "hint", "No timed runs yet. Latency appears here after the first full run."));
      return;
    }
    const slowest = Math.max(...perf.phase_stats.map((item) => item.average_seconds), 1);
    const box = el("div", "bars");
    for (const item of perf.phase_stats) {
      box.append(barRow(`${title(item.phase)} · ${item.runs} run${item.runs === 1 ? "" : "s"}`,
        `avg ${item.average_seconds.toFixed(1)}s`,
        `latest ${item.latest_seconds.toFixed(1)}s${item.slow ? " · slower than usual" : ""}`,
        item.average_seconds / slowest, item.slow ? "is-warn" : ""));
    }
    panel.append(box);

    const anySlow = perf.phase_stats.some((item) => item.slow);
    const callout = el("div", `callout ${anySlow ? "warn" : "ok"}`);
    callout.append(el("strong", null, anySlow ? "Latency recommendations" : "Latency"));
    const ul = el("ul");
    for (const rec of perf.recommendations) ul.append(el("li", null, rec));
    callout.append(ul);
    panel.append(callout);
  }

  const SKIP_REASON_LABEL = {
    off_target: "Title is not one of your target roles",
    work_mode: "Work arrangement you did not select",
    not_remote: "Not a remote role",
    outside_onsite_countries: "On-site outside your onsite countries, or location unknown",
    outside_remote_eligibility: "Remote role restricted to other countries",
    below_min_salary: "Pays below your minimum salary",
    too_old: "Older than your freshness window",
    undated: "Company listing with no posting date",
    incomplete_row: "Missing title, company or link",
    invalid_row: "Could not be read",
    duplicate_in_sweep: "Same role found again on another board or search",
    already_seen: "Found by an earlier search (already scored or tailored)",
  };

  // Every job a filter dropped, grouped by reason, so a missing job has an explanation.
  function appendSkippedJobs(panel) {
    const box = el("div", "skipped-jobs");
    panel.append(box);
    fetch("/api/skipped?limit=1000").then((r) => (r.ok ? r.json() : null)).then((data) => {
      if (!data || !data.total || !panel.contains(box)) return;
      box.append(el("h4", null, `Why ${data.total} listing${data.total === 1 ? " was" : "s were"} dropped`));
      Object.entries(data.reasons).sort((x, y) => y[1] - x[1]).forEach(([reason, count]) => {
        const group = el("details", "skip-group");
        group.append(el("summary", null, `${SKIP_REASON_LABEL[reason] || reason} — ${count}`));
        const list = el("ul", "list");
        data.jobs.filter((job) => job.reason === reason).slice(0, 15).forEach((job) => {
          const li = el("li");
          const label = [job.company, job.title].filter(Boolean).join(" — ") || "(untitled listing)";
          if (/^https?:\/\//i.test(job.url || "")) {
            const link = el("a", "link grow", label);
            link.href = job.url; link.target = "_blank"; link.rel = "noopener noreferrer";
            li.append(link);
          } else {
            li.append(el("span", "grow", label));
          }
          if (job.location) li.append(el("span", "metric", job.location));
          list.append(li);
        });
        if (count > list.children.length) list.append(el("li", null, `…and ${count - list.children.length} more`));
        group.append(list);
        box.append(group);
      });
    }).catch(() => {});
  }

  function renderPanel() {
    const panel = $("#panel-details");
    if (!state || !state.phases[selected]) return;
    const id = selected;
    const phase = state.phases[id];
    const status = nodeStatus(id);
    panel.innerHTML = "";

    panel.append(el("h3", null, phase.title));
    panel.append(el("p", null, phase.detail));

    const callout = el("div", `callout ${status === "ready" ? "ok" : status === "error" ? "err" : status === "halted" ? "warn" : ""}`);
    callout.append(el("strong", null, `${STATUS_LABEL[status] || status} — `), document.createTextNode(phase.summary));
    if (phase.hint) { callout.append(el("div", null, phase.hint)); }
    panel.append(callout);

    // Metrics
    if (Object.keys(phase.metrics || {}).length) {
      panel.append(el("h4", null, "Metrics"));
      const dl = el("dl", "kv");
      Object.entries(phase.metrics).forEach(([key, value]) => {
        dl.append(el("dt", null, key), el("dd", null, String(value)));
      });
      panel.append(dl);
    }

    // Phase-specific extras
    if (id === "intake" && phase.gaps?.length) {
      panel.append(el("h4", null, "Gaps to fill"));
      const box = el("div", "callout warn");
      const list = el("ul");
      phase.gaps.forEach((gap) => list.append(el("li", null, gap)));
      box.append(list);
      panel.append(box);
    }

    if (id === "source" && phase.breakdown && Object.keys(phase.breakdown).length) {
      panel.append(el("h4", null, "Delta store"));
      const dl = el("dl", "kv");
      Object.entries(phase.breakdown).forEach(([key, value]) => {
        dl.append(el("dt", null, key), el("dd", null, String(value)));
      });
      panel.append(dl);
    }

    if (id === "source") { appendSkippedJobs(panel); }

    if (id === "evaluate" && phase.top?.length) {
      panel.append(el("h4", null, "Top matches"));
      const list = el("ul", "list");
      phase.top.forEach((item) => {
        const li = el("li");
        li.append(el("span", "score", item.score.toFixed(1)));
        const link = el("a", "link grow", `${item.company} — ${item.title}`);
        link.href = item.url; link.target = "_blank"; link.rel = "noopener noreferrer";
        li.append(link);
        list.append(li);
      });
      panel.append(list);
    }

    if (id === "tailor" && phase.resumes?.length) {
      panel.append(el("h4", null, "Compiled resumes"));
      const list = el("ul", "list");
      phase.resumes.forEach((item) => {
        const li = el("li");
        li.append(el("span", "score", Number(item.score).toFixed(1)));
        li.append(el("span", "grow", `${item.company} — ${item.title}`));
        if (item.check) {
          const badge = el("span", "metric", item.check_passed ? "✓ validated" : "✗ check failed");
          badge.title = [item.check, ...(item.changes || [])].join("\n");
          li.append(badge);
        }
        if (item.pdf_path) {
          const open = el("a", "link", "Open PDF");
          open.href = `/api/file?path=${encodeURIComponent(item.pdf_path)}`;
          open.target = "_blank";
          open.rel = "noopener";
          li.append(open);
        }
        if (item.blocked?.length) {
          const flag = el("span", "metric", `${item.blocked.length} blocked`);
          flag.title = `Fabricated metrics removed: ${item.blocked.join(", ")}`;
          li.append(flag);
        }
        list.append(li);
      });
      panel.append(list);
    }

    // Artifacts
    const artifacts = (phase.artifacts || []).filter((a) => a.exists);
    if (artifacts.length) {
      panel.append(el("h4", null, "Output files"));
      const list = el("ul", "list");
      artifacts.forEach((artifact) => {
        const li = el("li");
        const link = el("a", "link grow", artifact.name);
        link.href = `/api/file?path=${encodeURIComponent(artifact.path)}`;
        link.target = "_blank"; link.rel = "noopener noreferrer";
        li.append(link, el("span", "metric", formatBytes(artifact.size)));
        list.append(li);
      });
      panel.append(list);
    }

    // Last run summary
    if (runSummary[id] && Object.keys(runSummary[id]).length) {
      panel.append(el("h4", null, "Last run"));
      const dl = el("dl", "kv");
      Object.entries(runSummary[id]).forEach(([key, value]) => {
        dl.append(el("dt", null, key.replace(/_/g, " ")), el("dd", null, formatValue(value)));
      });
      panel.append(dl);
    }

    panel.append(el("h4", null, "Equivalent command"));
    panel.append(el("code", "cmd", phase.command));

    const runBtn = el("button", "btn btn-primary", `Run ${phase.title.toLowerCase()} only`);
    runBtn.style.width = "100%";
    runBtn.style.marginTop = "14px";
    runBtn.disabled = running;
    runBtn.addEventListener("click", () => startRun([id]));
    panel.append(runBtn);
  }

  const fmtTime = (ts) => new Date(ts).toLocaleTimeString(undefined, { hour12: false });

  // Splits `text` into plain/matched chunks around a case-insensitive query,
  // so the filter reads as a highlight rather than just a narrowed list.
  function highlightInto(container, text, query) {
    if (!query) { container.append(document.createTextNode(text)); return; }
    const lower = text.toLowerCase();
    let i = 0;
    let at;
    while ((at = lower.indexOf(query, i)) !== -1) {
      if (at > i) container.append(document.createTextNode(text.slice(i, at)));
      const mark = el("mark", null, text.slice(at, at + query.length));
      container.append(mark);
      i = at + query.length;
    }
    if (i < text.length) container.append(document.createTextNode(text.slice(i)));
  }

  function filteredLogLines() {
    const query = logFilter.query.trim().toLowerCase();
    return logLines.filter((entry) => {
      const level = logLevel(entry);
      if (logFilter.level === "warn" && level !== "warn") return false;
      if (logFilter.level === "err" && level !== "err") return false;
      if (query && !entry.line.toLowerCase().includes(query) && !(entry.phase || "").toLowerCase().includes(query)) return false;
      return true;
    });
  }

  function renderLog() {
    const body = $("#log-body");
    if (!body) return;
    const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 60;
    const visible = filteredLogLines();
    const query = logFilter.query.trim().toLowerCase();

    body.innerHTML = "";
    if (!logLines.length) {
      body.append(el("span", "log-empty", "No activity yet. Press “Run all phases” to start."));
    } else if (!visible.length) {
      body.append(el("span", "log-empty", "No log lines match this filter."));
    } else {
      const frag = document.createDocumentFragment();
      for (const entry of visible) {
        const level = logLevel(entry);
        const line = el("div", `log-line ${level}`);
        line.append(el("span", "log-time", fmtTime(entry.ts)));
        // A visible gap is latency made legible: the seconds between two lines
        // are exactly the seconds the backend spent waiting (a Groq cooldown, a
        // scrape, a PDF compile) before it had anything new to say.
        if (entry.gapMs >= 1500) {
          const badge = el("span", `log-gap${entry.gapMs >= 5000 ? " log-gap-hot" : ""}`, `⏱ +${(entry.gapMs / 1000).toFixed(1)}s`);
          badge.title = "Time since the previous log line";
          line.append(badge);
        }
        if (entry.phase) line.append(el("span", "log-phase", entry.phase));
        const text = el("span", "log-text");
        highlightInto(text, entry.line, query);
        line.append(text);
        frag.append(line);
      }
      body.append(frag);
    }

    const count = $("#log-count");
    if (count) {
      count.textContent = visible.length === logLines.length
        ? `${logLines.length} line${logLines.length === 1 ? "" : "s"}`
        : `${visible.length} of ${logLines.length}`;
    }

    // Only auto-scroll when the user was already following the tail; otherwise
    // surface a "jump to latest" affordance instead of yanking their scroll
    // position while they're reading something further up.
    const jump = $("#log-jump");
    if (atBottom) {
      body.scrollTop = body.scrollHeight;
      if (jump) jump.hidden = true;
    } else if (jump && logLines.length) {
      jump.hidden = false;
    }
  }

  /* ---------------- Settings panel ---------------- */

  function renderSettings() {
    const panel = $("#panel-settings");
    if (!state) return;
    panel.innerHTML = "";

    /* --- Resume --- */
    panel.append(el("h3", null, "Input: resume"));
    panel.append(el("p", null, "Phase 1 reads this PDF. Everything downstream is built from it."));

    const drop = el("div", "dropzone",
      `Drop a resume here, or click to choose (${(state.formats || [".pdf"]).join(", ")})`);
    const picker = el("input");
    picker.type = "file"; picker.accept = acceptedFormats(); picker.hidden = true;
    drop.addEventListener("click", () => picker.click());
    picker.addEventListener("change", () => { if (picker.files[0]) uploadResume(picker.files[0]); });
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("is-over"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("is-over"));
    drop.addEventListener("drop", (e) => {
      e.preventDefault(); drop.classList.remove("is-over");
      const file = e.dataTransfer.files[0];
      if (file) uploadResume(file);
    });
    panel.append(drop, picker);

    if (state.resumes.length) {
      const field = el("div", "field");
      field.style.marginTop = "12px";
      field.append(el("label", null, "Use this resume"));
      const select = el("select");
      select.id = "resume-pick";
      state.resumes.forEach((resume) => {
        const option = el("option", null, `${resume.name} (${formatBytes(resume.size)})`);
        option.value = resume.name;
        select.append(option);
      });
      select.addEventListener("change", render);
      field.append(select);
      panel.append(field);
    }

    /* --- AI provider --- */
    panel.append(el("h4", null, "AI provider"));
    const llm = state.llm || {};
    const llmBox = el("div", `callout ${llm.active_provider && llm.active_provider !== "none" ? "ok" : "warn"}`);
    llmBox.textContent = llm.active_provider && llm.active_provider !== "none"
      ? `Active LLM: ${llm.active_provider}. Keys are saved locally in .env and are never shown back in the browser.`
      : "No LLM is active. The agent will use deterministic fallbacks until you add a provider key.";
    panel.append(llmBox);

    const llmForm = el("form");
    llmForm.id = "llm-form";
    const providerField = el("div", "field");
    providerField.append(el("label", null, "Provider"));
    const providerSelect = el("select");
    providerSelect.name = "provider";
    [
      ["groq", "Groq"],
      ["openai", "OpenAI"],
      ["anthropic", "Anthropic"],
      ["openai_compatible", "Other OpenAI-compatible API"],
      ["none", "None / deterministic fallback"],
    ].forEach(([value, label]) => {
      const option = el("option", null, label);
      option.value = value;
      option.selected = (llm.default_provider || llm.active_provider || "none") === value;
      providerSelect.append(option);
    });
    providerField.append(providerSelect);
    providerField.append(el("div", "hint", "Changing provider affects intake, scoring, resume tailoring, form answers, and outreach drafts."));
    llmForm.append(providerField);

    const providerBlocks = el("div", "provider-blocks");
    providerBlocks.append(providerPanel("groq", [
      passwordField("groq_api_keys", "Groq key(s)", "", llm.has_keys?.groq ? "Saved. Leave blank to keep existing key(s)." : "Paste one key, or multiple keys separated by commas."),
      textField("groq_model", "Groq model", llm.models?.groq || "openai/gpt-oss-120b"),
      textField("groq_fallback_model", "Fallback model", llm.models?.groq_fallback || "openai/gpt-oss-20b"),
    ]));
    providerBlocks.append(providerPanel("openai", [
      passwordField("openai_api_key", "OpenAI API key", "", llm.has_keys?.openai ? "Saved. Leave blank to keep existing key." : "Paste your OpenAI API key."),
      textField("openai_model", "OpenAI model", llm.models?.openai_tailor || "gpt-4o"),
    ]));
    providerBlocks.append(providerPanel("anthropic", [
      passwordField("anthropic_api_key", "Anthropic API key", "", llm.has_keys?.anthropic ? "Saved. Leave blank to keep existing key." : "Paste your Anthropic API key."),
      textField("anthropic_model", "Anthropic model", llm.models?.anthropic || "claude-sonnet-5"),
    ]));
    providerBlocks.append(providerPanel("openai_compatible", [
      passwordField("openai_compatible_api_key", "API key", "", llm.has_keys?.openai_compatible ? "Saved. Leave blank to keep existing key." : "Paste the provider key."),
      textField("openai_compatible_base_url", "Base URL", llm.openai_compatible_base_url || "", "Example: https://api.example.com/v1"),
      textField("openai_compatible_model", "Model", llm.models?.openai_compatible || "", "Use the model name from that provider."),
    ]));
    providerBlocks.append(providerPanel("none", [
      el("div", "hint", "No key needed. Intake, scoring, tailoring, and outreach use deterministic fallback logic."),
    ]));
    llmForm.append(providerBlocks);

    const strictLabel = el("label", "check");
    const strictInput = el("input");
    strictInput.type = "checkbox"; strictInput.name = "llm_strict";
    strictInput.checked = !!llm.strict;
    strictLabel.append(strictInput, el("span", null, "Stop the run when the LLM fails"));
    const strictField = el("div", "field");
    strictField.append(strictLabel);
    strictField.append(el("div", "hint", "Usually leave this off so a bad key falls back to deterministic output."));
    llmForm.append(strictField);

    const saveLLM = el("button", "btn btn-primary", "Save AI provider");
    saveLLM.type = "submit";
    saveLLM.style.width = "100%";
    llmForm.append(saveLLM);
    const updateProviderPanels = () => {
      providerBlocks.querySelectorAll("[data-provider-panel]").forEach((block) => {
        block.hidden = block.dataset.providerPanel !== providerSelect.value;
      });
    };
    providerSelect.addEventListener("change", updateProviderPanels);
    llmForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      await saveLLMSettings(llmForm);
    });
    panel.append(llmForm);
    updateProviderPanels();

    /* --- Search parameters --- */
    panel.append(el("h4", null, "Input: search parameters"));
    const cfg = state.config;
    if (cfg.status === "error") {
      const box = el("div", "callout err", cfg.error);
      panel.append(box);
    }
    if (cfg.warnings?.length) {
      const box = el("div", "callout warn");
      box.append(el("strong", null, "Check these before running:"));
      const list = el("ul");
      cfg.warnings.forEach((warning) => list.append(el("li", null, warning)));
      box.append(list);
      panel.append(box);
    }
    const values = cfg.values || {};

    const form = el("form");
    form.id = "config-form";

    form.append(textField("target_domains", "Target roles (comma separated)",
      (values.target_domains || []).join(", "), "Each role is searched on every board."));
    form.append(textField("locations", "Locations (comma separated)",
      (values.locations || []).join(", "), "Use “Remote” for remote-first searches."));

    const row1 = el("div", "field-row");
    row1.append(numberField("hours_old", "Posting age (hours)", values.hours_old ?? 48));
    row1.append(numberField("max_results_per_board", "Max per board", values.max_results_per_board ?? 25));
    form.append(row1);

    const row2 = el("div", "field-row");
    row2.append(numberField("min_salary", "Minimum salary", values.min_salary ?? "", true));
    row2.append(textField("salary_currency", "Salary currency", values.salary_currency || "USD",
      "e.g. INR or USD. The floor only applies to jobs listed in this currency."));
    form.append(row2);

    form.append(textField("country_indeed", "Indeed / Glassdoor country", values.country_indeed || "usa",
      "Must match your locations, e.g. india for Indian cities."));
    form.append(textField("onsite_countries", "Countries for onsite / hybrid roles",
      (values.onsite_countries || []).join(", "),
      "Optional. Only confirmed locations in these countries are kept for onsite and hybrid roles."));

    const modesField = el("div", "field");
    modesField.append(el("label", null, "Work arrangements"));
    const modeChecks = el("div", "checks");
    const effectiveModes = values.work_modes ||
      (values.is_remote !== false
        ? ((values.onsite_countries || []).length ? ["remote", "hybrid", "onsite"] : ["remote"])
        : ["remote", "hybrid", "onsite"]);
    [["remote", "Remote"], ["hybrid", "Hybrid"], ["onsite", "Onsite"]].forEach(([mode, title]) => {
      const label = el("label", "check");
      const input = el("input");
      input.type = "checkbox"; input.value = mode; input.name = "work_modes";
      input.checked = effectiveModes.includes(mode);
      label.append(input, el("span", null, title));
      modeChecks.append(label);
    });
    modesField.append(modeChecks);
    modesField.append(el("div", "hint", "Choose any combination. Worldwide remote eligibility is set below."));
    form.append(modesField);

    const boardsField = el("div", "field");
    boardsField.append(el("label", null, "Job boards"));
    const checks = el("div", "checks");
    ["linkedin", "indeed", "glassdoor", "zip_recruiter", "google", "bayt", "naukri", "bdjobs"].forEach((board) => {
      const label = el("label", "check");
      const input = el("input");
      input.type = "checkbox"; input.value = board; input.name = "job_boards";
      input.checked = (values.job_boards || []).includes(board);
      label.append(input, el("span", null, board));
      checks.append(label);
    });
    boardsField.append(checks);
    boardsField.append(el("div", "hint", "Glassdoor and ZipRecruiter return 403 without a residential proxy."));
    form.append(boardsField);

    const feedsField = el("div", "field");
    feedsField.append(el("label", null, "Public job APIs"));
    const feedChecks = el("div", "checks");
    [["remotive", "Remotive (remote)"], ["arbeitnow", "Arbeitnow"], ["jobicy", "Jobicy (remote)"]].forEach(([source, title]) => {
      const label = el("label", "check");
      const input = el("input");
      input.type = "checkbox"; input.value = source; input.name = "public_sources";
      input.checked = (values.public_sources || []).includes(source);
      label.append(input, el("span", null, title));
      feedChecks.append(label);
    });
    feedsField.append(feedChecks);
    feedsField.append(el("div", "hint", "Public JSON feeds add coverage without browser scraping or logins."));
    form.append(feedsField);

    form.append(el("h4", null, "Direct company career boards"));
    form.append(el("div", "hint", "Optional board tokens, comma-separated. These use public Greenhouse, Lever, and Ashby endpoints."));
    const atsRow = el("div", "field-row");
    atsRow.append(textField("ats_greenhouse", "Greenhouse tokens", (values.ats_companies?.greenhouse || []).join(", ")));
    atsRow.append(textField("ats_lever", "Lever tokens", (values.ats_companies?.lever || []).join(", ")));
    form.append(atsRow);
    form.append(textField("ats_ashby", "Ashby tokens", (values.ats_companies?.ashby || []).join(", ")));

    const contactsField = el("div", "field");
    const contactsLabel = el("label", "check");
    const contactsInput = el("input");
    contactsInput.type = "checkbox"; contactsInput.id = "find_contacts";
    contactsInput.checked = values.find_contacts !== false;
    contactsLabel.append(contactsInput, el("span", null, "Find published HR / careers emails"));
    contactsField.append(contactsLabel);
    contactsField.append(el("div", "hint",
      "Checks each employer's own website (and Hunter.io if HUNTER_API_KEY is set). " +
      "Only published addresses are kept; nothing is guessed. Results go to jobs_master.csv."));
    form.append(contactsField);

    const save = el("button", "btn btn-primary", "Save search parameters");
    save.type = "submit";
    save.style.width = "100%";
    form.append(save);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      await saveConfig(form);
    });
    panel.append(form);

    /* --- Candidate preferences --- */
    panel.append(el("h4", null, "Candidate preferences"));
    panel.append(el("div", "hint",
      "Not on a resume, but asked by application forms. Saved once and kept when you upload a new resume."));
    const prefs = state.preferences?.values || {};
    const prefForm = el("form");
    prefForm.id = "prefs-form";
    const prow1 = el("div", "field-row");
    prow1.append(textField("current_country", "Country you live in", prefs.current_country || ""));
    prow1.append(textField("authorized_countries", "Allowed to work in (no visa needed)",
      (prefs.authorized_countries || []).join(", ")));
    prefForm.append(prow1);

    const choice = (name, label, value, yes, no) => {
      const field = el("div", "field");
      field.append(el("label", null, label));
      const select = el("select");
      select.name = name;
      [["", "Not stated"], ["true", yes], ["false", no]].forEach(([v, text]) => {
        const option = el("option", null, text);
        option.value = v;
        option.selected = String(value ?? "") === v;
        select.append(option);
      });
      field.append(select);
      return field;
    };
    prefForm.append(choice("requires_sponsorship", "Visa sponsorship", prefs.requires_sponsorship,
      "Needed only to work in other countries", "Never needed"));
    prefForm.append(choice("remote_worldwide", "Remote work", prefs.remote_worldwide,
      "Open to remote jobs from any country", "Not open to foreign remote jobs"));

    const prow2 = el("div", "field-row");
    prow2.append(numberField("desired_salary", "Expected salary from (per year)", prefs.desired_salary ?? "", true));
    prow2.append(numberField("desired_salary_max", "up to", prefs.desired_salary_max ?? "", true));
    prow2.append(textField("pref_currency", "Currency", prefs.salary_currency || "INR"));
    prefForm.append(prow2);
    prefForm.append(el("div", "hint", "10–14 LPA is 1000000 to 1400000 INR."));

    const savePrefs = el("button", "btn btn-primary", "Save preferences");
    savePrefs.type = "submit";
    savePrefs.style.width = "100%";
    prefForm.append(savePrefs);
    prefForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const data = new FormData(prefForm);
      const bool = (key) => (data.get(key) === "" ? null : data.get(key) === "true");
      const num = (key) => { const v = String(data.get(key) || "").trim(); return v === "" ? null : Number(v); };
      const payload = {
        current_country: String(data.get("current_country") || "").trim() || null,
        authorized_countries: String(data.get("authorized_countries") || "").split(",").map((s) => s.trim()).filter(Boolean),
        requires_sponsorship: bool("requires_sponsorship"),
        remote_worldwide: bool("remote_worldwide"),
        desired_salary: num("desired_salary"),
        desired_salary_max: num("desired_salary_max"),
        salary_currency: String(data.get("pref_currency") || "").trim() || null,
      };
      try {
        const result = await api("/api/preferences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        state = result.state;
        render(); renderSettings();
        toast("Preferences saved to your profile.", "ok");
      } catch (err) {
        toast(err.message, "err");
      }
    });
    panel.append(prefForm);

    runOptionsSection(panel);

    /* --- Where things land --- */
    panel.append(el("h4", null, "Output locations"));
    const dl = el("dl", "kv");
    dl.append(el("dt", null, "Artifacts"), el("dd", null, state.paths.outputs));
    dl.append(el("dt", null, "Resumes"), el("dd", null, state.paths.resumes));
    dl.append(el("dt", null, "Tracker"), el("dd", null, state.paths.tracker));
    dl.append(el("dt", null, "Jobs CSV"), el("dd", null, state.paths.jobs_csv));
    panel.append(dl);
    groupSections(panel);
  }

  // The settings form is long, so each block becomes a collapsible section.
  // Open/closed state lives here (not in the DOM) because saving re-renders
  // the whole panel and would otherwise snap everything back.
  const settingsOpen = new Set(["Resume", "Search parameters"]);
  const SECTION_TITLES = { "Input: resume": "Resume", "Input: search parameters": "Search parameters" };

  function groupSections(panel) {
    const nodes = [...panel.children];
    panel.replaceChildren();
    let body = null;
    for (const node of nodes) {
      if (node.matches("h3, h4")) {
        const name = SECTION_TITLES[node.textContent] || node.textContent;
        const details = el("details", "sec");
        details.open = settingsOpen.has(name);
        details.addEventListener("toggle", () => {
          if (details.open) settingsOpen.add(name); else settingsOpen.delete(name);
        });
        const summary = el("summary");
        summary.append(el("span", null, name), icon("chevron"));
        body = el("div", "sec-body");
        details.append(summary, body);
        panel.append(details);
      } else if (body) {
        body.append(node);
      } else {
        panel.append(node);
      }
    }
  }

  function textField(name, label, value, hint) {
    const field = el("div", "field");
    field.append(el("label", null, label));
    const input = el("input");
    input.type = "text"; input.name = name; input.value = value ?? "";
    field.append(input);
    if (hint) field.append(el("div", "hint", hint));
    return field;
  }

  function passwordField(name, label, value, hint) {
    const field = textField(name, label, value, hint);
    field.querySelector("input").type = "password";
    field.querySelector("input").autocomplete = "off";
    return field;
  }

  function providerPanel(name, children) {
    const block = el("div", "provider-panel");
    block.dataset.providerPanel = name;
    children.forEach((child) => block.append(child));
    return block;
  }

  function numberField(name, label, value, optional) {
    const field = el("div", "field");
    field.append(el("label", null, label));
    const input = el("input");
    input.type = "number"; input.name = name; input.value = value ?? "";
    if (optional) input.placeholder = "any";
    field.append(input);
    return field;
  }

  async function saveLLMSettings(form) {
    const data = new FormData(form);
    const payload = {
      provider: String(data.get("provider") || "none"),
      groq_api_keys: String(data.get("groq_api_keys") || "").trim(),
      groq_model: String(data.get("groq_model") || "").trim(),
      groq_fallback_model: String(data.get("groq_fallback_model") || "").trim(),
      openai_api_key: String(data.get("openai_api_key") || "").trim(),
      openai_model: String(data.get("openai_model") || "").trim(),
      anthropic_api_key: String(data.get("anthropic_api_key") || "").trim(),
      anthropic_model: String(data.get("anthropic_model") || "").trim(),
      openai_compatible_api_key: String(data.get("openai_compatible_api_key") || "").trim(),
      openai_compatible_base_url: String(data.get("openai_compatible_base_url") || "").trim(),
      openai_compatible_model: String(data.get("openai_compatible_model") || "").trim(),
      llm_strict: data.has("llm_strict"),
    };
    try {
      const result = await api("/api/llm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      state = result.state;
      render(); renderSettings();
      toast(result.provider && result.provider !== "none"
        ? `AI provider active: ${result.provider}`
        : "AI provider disabled; deterministic fallbacks active.", "ok");
    } catch (err) {
      toast(err.message, "err");
    }
  }

  async function saveConfig(form) {
    const data = new FormData(form);
    const split = (key) =>
      String(data.get(key) || "").split(",").map((s) => s.trim()).filter(Boolean);
    const salary = String(data.get("min_salary") || "").trim();

    const payload = {
      target_domains: split("target_domains"),
      locations: split("locations"),
      job_boards: data.getAll("job_boards"),
      public_sources: data.getAll("public_sources"),
      work_modes: data.getAll("work_modes"),
      hours_old: Number(data.get("hours_old")) || 48,
      max_results_per_board: Number(data.get("max_results_per_board")) || 25,
      country_indeed: String(data.get("country_indeed") || "usa"),
      min_salary: salary === "" ? null : Number(salary),
      salary_currency: String(data.get("salary_currency") || "USD"),
      is_remote: data.getAll("work_modes").length === 1 && data.getAll("work_modes")[0] === "remote",
      find_contacts: $("#find_contacts").checked,
      onsite_countries: split("onsite_countries"),
      desired_experience_years: state.config.values?.desired_experience_years ?? 3.0,
      ats_companies: Object.fromEntries(Object.entries({
        greenhouse: split("ats_greenhouse"),
        lever: split("ats_lever"),
        ashby: split("ats_ashby"),
      }).filter(([, tokens]) => tokens.length)),
      proxy_url: state.config.values?.proxy_url ?? null,
    };

    try {
      const result = await api("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      state = result.state;
      render(); renderSettings();
      toast("Search parameters saved.", "ok");
    } catch (err) {
      toast(err.message, "err");
    }
  }

  /**
   * Upload a resume PDF.
   *
   * @param {File} file
   * @param {boolean} thenParse Parse it immediately. The setup wizard passes true
   *   so uploading is a single action rather than upload-then-remember-to-run.
   */
  async function uploadResume(file, thenParse = false) {
    const formats = state?.formats || [".pdf"];
    if (!formats.some((suffix) => file.name.toLowerCase().endsWith(suffix))) {
      toast(`Unsupported file. Accepted: ${formats.join(", ")}`, "err");
      return;
    }
    try {
      const result = await api("/api/resume", {
        method: "POST",
        headers: { "X-Filename": encodeURIComponent(file.name), "Content-Type": "application/pdf" },
        body: file,
      });
      state = result.state;
      setup.picked = result.name;
      render();
      if (!$("#panel-settings").hidden) renderSettings();

      if (thenParse) {
        runIntakeFromSetup(result.name);
      } else {
        toast(`Uploaded ${result.name}. Run phase 1 to parse it.`, "ok");
        if (setup.open) renderSetup();
      }
    } catch (err) {
      toast(err.message, "err");
    }
  }

  /* ---------------- Helpers ---------------- */

  function formatBytes(bytes) {
    if (!bytes) return "0 B";
    const units = ["B", "KB", "MB"];
    const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    return `${(bytes / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
  }

  function formatValue(value) {
    if (Array.isArray(value)) return value.length ? value.join(", ") : "none";
    if (value && typeof value === "object") {
      const entries = Object.entries(value);
      return entries.length ? entries.map(([k, v]) => `${k}: ${v}`).join(", ") : "none";
    }
    return String(value);
  }

  /* ---------------- Wiring ---------------- */

  let jobRows = [];
  let outreachDirectory = "";
  let manuallyApplied = new Set();
  let visibleJobs = 50;
  function safeLink(label, url) {
    const link = el("a", "link", label);
    if (/^https?:\/\//i.test(url || "") || (url || "").startsWith("/api/file?")) {
      link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer";
    }
    return link;
  }
  // Jobs the candidate chose to skip. Recorded on the server (not only hidden here) so the
  // next search does not resurface them and evaluation does not spend quota scoring them
  // again. It is still not an application outcome: nothing reaches the tracker.
  const skippedJobs = new Set();
  // Skips made before they were stored on the server lived in this browser only; send them
  // once, then forget the local copy.
  async function migrateLocalSkips(knownIds) {
    let local = [];
    try { local = JSON.parse(localStorage.getItem("jobAgentSkipped") || "[]"); } catch { return; }
    if (!local.length) return;
    for (const id of local) {
      if (skippedJobs.has(id) || !knownIds.has(id)) continue;
      try { await api("/api/jobs/skip", {method: "POST", body: JSON.stringify({job_id: id})}); skippedJobs.add(id); }
      catch { /* an unknown or already-recorded job: nothing to carry over */ }
    }
    try { localStorage.removeItem("jobAgentSkipped"); } catch { /* private mode */ }
  }
  let openJobId = null;

  function fitTone(score) {
    const n = Number(score);
    if (score === "" || score == null || !Number.isFinite(n)) return "none";
    return n >= 7 ? "good" : n >= 5 ? "mid" : "low";
  }
  const FIT_WORD = { good: "strong fit", mid: "possible fit", low: "weak fit", none: "not scored" };

  function fitBadge(row) {
    const tone = fitTone(row["Fit Score"]);
    const badge = el("span", `fit fit-${tone}`, tone === "none" ? "—" : String(row["Fit Score"]));
    badge.setAttribute("aria-label", tone === "none" ? "Not scored" : `Fit ${row["Fit Score"]} out of 10, ${FIT_WORD[tone]}`);
    return badge;
  }

  function chipList(text, cls) {
    const wrap = el("div", "chips");
    String(text || "").split(/[,;]\s*/).map((s) => s.trim()).filter(Boolean)
      .forEach((item) => wrap.append(el("span", `chip ${cls || ""}`, item)));
    return wrap;
  }

  function drawerSection(title, ...children) {
    const section = el("section", "drawer-section");
    section.append(el("h4", null, title), ...children);
    return section;
  }

  function jobFileLink(row, column, label, download) {
    if (!row[column]) return null;
    return safeLink(label, `/api/file?${download ? "download=1&" : ""}path=${encodeURIComponent(row[column])}`);
  }

  function renderJobDrawer(row) {
    const drawer = $("#job-drawer");
    drawer.replaceChildren();
    if (!row) { openJobId = null; drawer.hidden = true; $(".jobs-layout").classList.remove("has-drawer"); return; }
    const id = row["Job ID"];
    openJobId = id;
    drawer.hidden = false;
    $(".jobs-layout").classList.add("has-drawer");

    const close = el("button", "btn btn-sm drawer-close", "Close");
    close.type = "button";
    close.setAttribute("aria-label", "Close job details");
    close.addEventListener("click", () => { renderJobDrawer(null); renderJobRows(); });

    const head = el("header", "drawer-head");
    const titleBox = el("div", "drawer-title");
    titleBox.append(el("h3", null, row.Title || "Untitled role"),
      el("p", "drawer-company", `${row.Company || "Unknown company"} · ${row.Location || "Unknown location"}${row["Work Mode"] ? ` · ${row["Work Mode"]}` : ""}`));
    head.append(fitBadge(row), titleBox, close);
    drawer.append(head);

    const primary = el("div", "drawer-primary");
    const apply = safeLink("View job / apply ↗", row["Apply URL"] || row["Job URL"]);
    apply.classList.add("btn", "btn-primary");
    primary.append(apply);
    drawer.append(primary);
    if (row["Next Step"]) drawer.append(el("p", "drawer-next", row["Next Step"]));
    if (row["Search Batch"] !== "Current search") drawer.append(el("p", "hint", "Earlier search; fit may be stale."));

    const why = [el("p", null, row["Score Reasoning"] || "No scoring explanation is recorded.")];
    if (row["Matched Skills"]) why.push(el("p", "hint", "Matched"), chipList(row["Matched Skills"], "chip-ok"));
    if (row["Missing Skills"]) why.push(el("p", "hint", "Reported gaps"), chipList(row["Missing Skills"], "chip-warn"));
    if (row["Skills Found In Profile"]) why.push(el("p", "hint", `Already in your profile: ${row["Skills Found In Profile"]}. Review the scorer's gap assessment; the score has not been changed.`));
    why.push(el("p", "hint", "Only add keywords supported by your actual experience."));
    drawer.append(drawerSection("Why this score", ...why));

    drawer.append(drawerSection("Eligibility",
      el("p", null, row["Remote Eligibility"] || "Review eligibility on the employer listing."),
      ...(row["Eligibility Notes"] ? [el("p", "hint", row["Eligibility Notes"])] : [])));

    const contact = [el("p", null, row["HR / Careers Email"] || "No published email found"),
      el("p", "hint", row["Email Verification"] || "Deliverability not checked")];
    if (row["Email Found On"]) contact.push(safeLink("Contact source", row["Email Found On"]));
    if (row["Possible Contacts"]) contact.push(el("p", "hint", `Possible contacts (unverified): ${row["Possible Contacts"]}`));
    drawer.append(drawerSection("Contact", ...contact));

    const files = el("div", "drawer-links");
    [jobFileLink(row, "Tailored Resume Path", "Open tailored PDF"),
     jobFileLink(row, "Tailored Resume Path", "Download resume", true),
     jobFileLink(row, "Cover Letter", "Cover letter", true),
     jobFileLink(row, "Interview Prep", "Interview prep", true)]
      .filter(Boolean).forEach((link) => files.append(link));
    if (row["Email Draft File"] && outreachDirectory) {
      files.append(safeLink("Open unsent email draft", `/api/file?path=${encodeURIComponent(outreachDirectory + "/" + row["Email Draft File"])}`));
    }
    drawer.append(drawerSection("Documents", el("p", "hint", row["Resume Check"] || "Resume not yet generated"), files));

    const actions = el("div", "drawer-actions");

    if (fitTone(row["Fit Score"]) === "none") {
      const retry = el("button", "btn btn-sm", "Retry evaluation");
      retry.type = "button"; retry.disabled = running;
      retry.title = "Re-runs the evaluation phase; jobs without a readable description are retried.";
      retry.addEventListener("click", () => { $("#jobs-dialog").close(); startRun(["evaluate"]); });
      actions.append(retry);
    }
    const resumeBtn = el("button", `btn btn-sm${row["Tailored Resume Path"] ? " btn-ghost" : " btn-primary"}`,
                         row["Tailored Resume Path"] ? "Regenerate resume" : "Generate tailored resume");
    resumeBtn.type = "button"; resumeBtn.disabled = running;
    resumeBtn.title = "Rebuilds your own resume PDF for this job: same design, content reordered for the role. Works with or without a job description.";
    resumeBtn.addEventListener("click", () => { $("#jobs-dialog").close(); startRun(["tailor"], id); });
    actions.prepend(resumeBtn);
    const minScore = Number(state?.thresholds?.min_match_score ?? 7);
    if (row["Tailored Resume Path"] && !row["Interview Prep"] && Number(row["Fit Score"]) >= minScore) {
      const prep = el("button", "btn btn-sm", "Prepare interview guide");
      prep.type = "button"; prep.disabled = running;
      prep.addEventListener("click", () => { $("#jobs-dialog").close(); startRun(["prep"], id); });
      actions.append(prep);
    }
    if (!String(row.Status || "").startsWith("replied_") && (row.Status !== "applied" || manuallyApplied.has(id))) {
      const undo = manuallyApplied.has(id);
      const mark = el("button", "btn btn-sm", undo ? "Undo applied marker" : "Mark as applied");
      mark.type = "button";
      mark.addEventListener("click", async () => {
        mark.disabled = true;
        try {
          const result = await api("/api/jobs/applied", {method: "POST", body: JSON.stringify({job_id: id, undo})});
          toast((result.warnings || []).join(" ") || (undo ? "Manual marker undone." : "Recorded. No application was sent by this action."), "ok");
          await openJobs();
        } catch (error) { toast(error.message, "err"); mark.disabled = false; }
      });
      actions.append(mark);
    }
    const skipped = skippedJobs.has(id);
    const skip = el("button", "btn btn-sm btn-ghost", skipped ? "Restore to shortlist" : "Skip this job");
    skip.type = "button";
    skip.title = "Removes this job from your shortlist and keeps it out of future searches and scoring. It is not recorded as an application.";
    skip.addEventListener("click", async () => {
      skip.disabled = true;
      try {
        await api("/api/jobs/skip", {method: "POST", body: JSON.stringify({job_id: id, undo: skipped})});
      } catch (error) { toast(error.message, "err"); skip.disabled = false; return; }
      if (skipped) skippedJobs.delete(id); else skippedJobs.add(id);
      toast(skipped ? "Job restored to your shortlist." : "Job skipped. It will not be scored or suggested again.", "ok");
      if (!skipped && !$("#jobs-skipped").checked) renderJobDrawer(null);
      renderJobRows();
    });
    actions.append(skip);
    const foot = el("div", "drawer-foot");
    foot.append(actions);
    drawer.append(foot);
  }

  // What a person needs to know about a job at a glance. Having a resume is not the same as
  // being worth applying to, so the three situations are named separately.
  const READINESS_LABEL = {
    "Ready for your review": "Ready to review",
    "Resume ready, not scored": "Resume · not scored",
    "Resume ready, below threshold": "Resume · below threshold",
  };

  function jobStatusText(row) {
    if (manuallyApplied.has(row["Job ID"])) return "Applied (marked)";
    if (READINESS_LABEL[row["Application Readiness"]]) return READINESS_LABEL[row["Application Readiness"]];
    const raw = String(row.Status || "found").replace(/_/g, " ");
    return raw.charAt(0).toUpperCase() + raw.slice(1);
  }

  function renderJobRows() {
    const query = $("#jobs-query").value.toLowerCase();
    const mode = $("#jobs-mode").value;
    const hiring = $("#jobs-email").checked;
    const current = $("#jobs-current").checked;
    const ready = $("#jobs-ready").checked;
    const showSkipped = $("#jobs-skipped").checked;
    const rows = jobRows.filter(row => (!mode || row["Work Mode"] === mode)
      && (!current || jobsRun || row["Search Batch"] === "Current search")
      && (!ready || row["Application Readiness"] === "Ready for your review")
      && (!hiring || ["hiring", "person"].includes(row["Email Type"]))
      && (showSkipped || !skippedJobs.has(row["Job ID"]))
      && ["Title", "Company", "Location"].some(key => (row[key] || "").toLowerCase().includes(query)))
      .sort((a, b) => (Number(b["Fit Score"]) || 0) - (Number(a["Fit Score"]) || 0));
    const hidden = jobRows.filter(r => skippedJobs.has(r["Job ID"])).length;
    $("#jobs-count").textContent = `${rows.length} matching · ${jobRows.length} saved · showing ${Math.min(visibleJobs, rows.length)} · fit is out of 10${hidden && !showSkipped ? ` · ${hidden} skipped` : ""}`;
    $("#jobs-more").hidden = rows.length <= visibleJobs;
    const body = $("#jobs-rows"); body.replaceChildren();
    for (const row of rows.slice(0, visibleJobs)) {
      const id = row["Job ID"];
      const tr = el("tr", `job-row${openJobId === id ? " is-open" : ""}${skippedJobs.has(id) ? " is-skipped" : ""}`);
      tr.tabIndex = 0;
      tr.setAttribute("aria-label", `${row.Title}, ${row.Company}. Open details.`);
      const fit = el("td", "col-fit"); fit.append(fitBadge(row));
      const role = el("td", "col-role");
      role.append(el("strong", null, row.Title), el("span", "sub", row.Company), el("span", "sub faint", row.Source || ""));
      const place = el("td", null, `${row.Location || "Unknown"}${row["Work Mode"] ? ` · ${row["Work Mode"]}` : ""}`);
      const contact = el("td", row["HR / Careers Email"] ? "" : "faint", row["HR / Careers Email"] ? "Email found" : "No email");
      const status = el("td"); status.append(el("span", `status-chip${manuallyApplied.has(id) ? " is-done" : row["Application Readiness"] === "Ready for your review" ? " is-ready" : ""}`, jobStatusText(row)));
      tr.append(fit, role, place, contact, status);
      const open = () => { renderJobDrawer(row); renderJobRows(); $("#job-drawer").scrollTop = 0; };
      tr.addEventListener("click", open);
      tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
      body.append(tr);
    }
    if (!rows.length) {
      const tr = el("tr");
      const td = el("td", "jobs-empty", jobRows.length
        ? "No jobs match these filters. Clear a filter to see more."
        : "No jobs yet. Run sourcing from the pipeline to find some.");
      td.colSpan = 5; tr.append(td); body.append(tr);
    }
    // A job that no longer exists (e.g. archived) must not leave a stale drawer.
    if (openJobId && !jobRows.some(r => r["Job ID"] === openJobId)) renderJobDrawer(null);
  }

  async function openJobs() {
    const dialog = $("#jobs-dialog"); if (!dialog.open) dialog.showModal();
    $("#jobs-count").textContent = "Loading jobs…";
    try {
      populateRunSelect();
      const result = await api(`/api/jobs${jobsRun ? `?run=${encodeURIComponent(jobsRun)}` : ""}`);
      jobRows = result.jobs || []; visibleJobs = 50;
      outreachDirectory = result.outreach_dir || "";
      manuallyApplied = new Set(result.manually_applied || []);
      skippedJobs.clear();
      (result.user_skipped || []).forEach((id) => skippedJobs.add(id));
      await migrateLocalSkips(new Set(jobRows.map((row) => row["Job ID"])));
      $("#jobs-csv").href = `/api/file?path=${encodeURIComponent(result.csv_path)}`;
      $("#jobs-latest-csv").href = `/api/file?path=${encodeURIComponent(result.latest_csv_path)}`;
      $("#jobs-ready-csv").href = `/api/file?path=${encodeURIComponent(result.ready_csv_path)}`;
      const report = result.run_report || {};
      $("#jobs-run-status").textContent = report.status
        ? `Run: ${report.status}${report.active_phase ? ` · ${report.active_phase}` : ""}. ${report.halt_reason || ""} ${[...(report.warnings || []), ...(result.warnings || [])].join(" ")}`
        : "No run report yet. Run the agent to refresh these saved results.";
      const coverage = result.coverage || {};
      const sources = [...Object.entries(coverage.boards || {}), ...Object.entries(coverage.public_feeds || {})];
      $("#jobs-coverage").textContent = coverage.checked_at
        ? `Last sweep: ${new Date(coverage.checked_at).toLocaleString()}. ${sources.map(([name, value]) => `${name}: ${value.status} (${value.matching_listings || 0})`).join(" · ")}.`
        : "No source coverage report yet. Existing jobs may come from an earlier run.";
      if (coverage.checked_at && Date.now() - new Date(coverage.checked_at).getTime() > (coverage.hours_old || 48) * 3600000) {
        $("#jobs-coverage").textContent += " These saved results are older than your search window. Run a fresh search before applying.";
      }
      renderJobRows();
      const open = openJobId && jobRows.find(r => r["Job ID"] === openJobId);
      if (open) renderJobDrawer(open);
    } catch (error) { $("#jobs-count").textContent = error.message; }
  }

  /* ---------------- Command palette and shortcuts ---------------- */

  // Every action here already exists as a button; the palette only gives
  // keyboard users one place to reach them. Runs go through the same handlers
  // as the buttons, so the live-apply confirmation and resume checks still apply.
  function paletteCommands() {
    const commands = [
      { title: "Open job shortlist", hint: "J", run: openJobs },
      { title: "Go to Details", run: () => switchTab("details") },
      { title: "Go to Live log", run: () => switchTab("log") },
      { title: "Go to History", run: () => switchTab("history") },
      { title: "Go to Settings", run: () => switchTab("settings") },
      { title: "Go to Analytics", run: () => switchTab("analytics") },
      { title: "Toggle light / dark theme", hint: "T", run: () => $("#theme-btn").click() },
      { title: "Toggle fit to view", run: () => $("#fit-btn").click() },
      { title: "Show keyboard shortcuts", hint: "?", run: openShortcuts },
    ];
    if (state && !running) {
      commands.unshift({ title: "Run all phases", run: () => $("#run-btn").click() });
      (state.order || []).forEach((id) => {
        commands.splice(1, 0, { title: `Run ${state.phases[id].title} only`, run: () => startRun([id]) });
      });
    }
    return commands;
  }

  const palette = { items: [], active: 0 };

  function openPalette() {
    const dialog = $("#palette");
    if (dialog.open) { dialog.close(); return; }
    $("#palette-input").value = "";
    renderPalette();
    dialog.showModal();
    $("#palette-input").focus();
  }

  function renderPalette() {
    const query = $("#palette-input").value.trim().toLowerCase();
    const words = query.split(/\s+/).filter(Boolean);
    palette.items = paletteCommands().filter((c) => words.every((w) => c.title.toLowerCase().includes(w)));
    palette.active = Math.min(palette.active, Math.max(0, palette.items.length - 1));
    const list = $("#palette-list");
    list.replaceChildren();
    palette.items.forEach((command, i) => {
      const item = el("li", `palette-item${i === palette.active ? " is-active" : ""}`);
      item.id = `palette-opt-${i}`;
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", String(i === palette.active));
      item.append(el("span", null, command.title));
      if (command.hint) item.append(el("kbd", null, command.hint));
      item.addEventListener("click", () => runPaletteItem(i));
      item.addEventListener("mousemove", () => { if (palette.active !== i) { palette.active = i; renderPalette(); } });
      list.append(item);
    });
    if (!palette.items.length) list.append(el("li", "palette-empty", "No matching command."));
    $("#palette-input").setAttribute("aria-activedescendant", palette.items.length ? `palette-opt-${palette.active}` : "");
    list.querySelector(".is-active")?.scrollIntoView({ block: "nearest" });
  }

  function runPaletteItem(index) {
    const command = palette.items[index];
    if (!command) return;
    $("#palette").close();
    // Let the dialog finish closing so a command that opens another dialog
    // (the shortlist) is not immediately dismissed.
    setTimeout(command.run, 0);
  }

  function openShortcuts() {
    const dialog = $("#shortcuts");
    if (!dialog.open) dialog.showModal();
  }

  function initPalette() {
    $("#palette-input").addEventListener("input", () => { palette.active = 0; renderPalette(); });
    $("#palette-input").addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const n = palette.items.length;
        if (n) { palette.active = (palette.active + (e.key === "ArrowDown" ? 1 : n - 1)) % n; renderPalette(); }
      } else if (e.key === "Enter") {
        e.preventDefault();
        runPaletteItem(palette.active);
      }
    });
    $("#palette").addEventListener("click", (e) => { if (e.target === $("#palette")) $("#palette").close(); });
    $("#shortcuts-close").addEventListener("click", () => $("#shortcuts").close());
    $("#shortcuts").addEventListener("click", (e) => { if (e.target === $("#shortcuts")) $("#shortcuts").close(); });

    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); return; }
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const target = e.target;
      const typing = target.matches?.("input, textarea, select, [contenteditable]");
      if (typing || $("#palette").open || $("#shortcuts").open) return;
      if (e.key === "?") { e.preventDefault(); openShortcuts(); }
      else if (e.key === "/") {
        e.preventDefault();
        if ($("#jobs-dialog").open) $("#jobs-query").focus();
        else { switchTab("log"); $("#log-search").focus(); }
      } else if (e.key.toLowerCase() === "j" && !$("#jobs-dialog").open && !$("#setup-root").children.length) openJobs();
      else if (e.key.toLowerCase() === "t") $("#theme-btn").click();
    });
  }

  /* ---------------- Last run summary ---------------- */

  function oneLine(summary) {
    const parts = [];
    for (const [key, value] of Object.entries(summary || {})) {
      if (["string", "number", "boolean"].includes(typeof value) && String(value).length <= 24 && !/path|tracker|note|warning/i.test(key)) {
        parts.push(`${key.replace(/_/g, " ")} ${value}`);
      }
      if (parts.length === 3) break;
    }
    return parts.join(" · ");
  }

  function lastRunCard() {
    const report = state?.run_report;
    if (!report || !report.started_at || !report.phases || !Object.keys(report.phases).length) return null;
    const card = el("section", "last-run");
    const verdict = { ok: "Completed", warning: "Completed with warnings", error: "Failed", interrupted: "Stopped" }[report.status] || report.status || "Unknown";
    const started = new Date(report.started_at), finished = report.finished_at ? new Date(report.finished_at) : null;
    const seconds = finished ? Math.max(0, (finished - started) / 1000) : null;
    const head = el("div", "last-run-head");
    head.append(el("strong", null, verdict), el("span", "hint", `${started.toLocaleString()}${seconds != null ? ` · ${formatElapsed(seconds * 1000)}` : ""}${report.dry_run ? " · dry run" : ""}`));
    card.append(head);
    const rows = el("ul", "last-run-phases");
    (state.order || Object.keys(report.phases)).forEach((id) => {
      const phase = report.phases[id];
      if (!phase) return;
      const li = el("li", `lr-${phase.status === "ok" ? "ok" : phase.status === "warning" ? "warn" : "err"}`);
      li.append(el("span", "lr-dot"), el("span", "lr-name", state.phases[id]?.title || title(id)), el("span", "lr-sum", oneLine(phase.summary) || (STATUS_LABEL[phase.status === "ok" ? "ready" : phase.status] || phase.status)));
      rows.append(li);
    });
    card.append(rows);
    if ((report.warnings || []).length) {
      const warn = el("div", "callout warn");
      report.warnings.slice(0, 3).forEach((w) => warn.append(el("div", null, w)));
      card.append(warn);
    }
    return card;
  }

  /* ---------------- Run history ---------------- */

  const RUN_VERDICT = { ok: "Completed", warning: "Completed with warnings", error: "Failed", halted: "Stopped",
                        cancelled: "Stopped", interrupted: "Interrupted", running: "Running" };
  let jobsRun = "";            // run whose jobs the shortlist shows ("" = every saved job)
  let historyRuns = [];
  let historyProfiles = [];        // [{key, name}] of everyone who has run on this machine
  let currentProfile = "";         // the candidate whose profile is loaded now
  let historyProfile = null;       // null = not chosen yet, which means the current candidate

  function runLabel(run) {
    const when = new Date(run.started_at);
    return `${when.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })} `
         + `${when.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}`;
  }

  function totalsLine(totals) {
    const t = totals || {};
    const parts = [];
    if (t.jobs_found != null) parts.push(`${t.jobs_found} found`);
    if (t.scored != null) parts.push(`${t.scored} scored`);
    if (t.qualified != null) parts.push(`${t.qualified} qualified`);
    if (t.resumes != null) parts.push(`${t.resumes} resumes`);
    if (t.submitted) parts.push(`${t.submitted} submitted`);
    return parts.join(" · ") || "No results recorded";
  }

  function runCard(run) {
    const card = el("article", "run-card");
    const verdict = RUN_VERDICT[run.status] || run.status || "Unknown";
    const tone = run.status === "ok" ? "ok" : run.status === "warning" ? "warn" : run.status === "running" ? "run" : "err";
    const head = el("div", "run-head");
    head.append(el("strong", null, new Date(run.started_at).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })),
                el("span", `run-status run-${tone}`, verdict));
    if (run.dry_run) head.append(el("span", "run-tag", "dry run"));
    card.append(head);
    const who = [run.candidate_name || "Unknown profile", run.resume_file].filter(Boolean).join(" · ");
    card.append(el("p", "run-who", who), el("p", "run-totals", totalsLine(run.totals)));

    const results = run.results || {};
    const names = Object.keys(results);
    if (names.length) {
      const details = el("details", "run-phases");
      const seconds = run.finished_at ? (new Date(run.finished_at) - new Date(run.started_at)) / 1000 : null;
      details.append(el("summary", null, `${names.length} phase${names.length === 1 ? "" : "s"}${seconds != null ? ` · ${formatElapsed(seconds * 1000)}` : ""}`));
      const list = el("ul");
      for (const name of names) {
        const item = results[name];
        const li = el("li", `lr-${item.status === "ok" ? "ok" : item.status === "warning" ? "warn" : "err"}`);
        li.append(el("span", "lr-dot"), el("span", "lr-name", title(name)),
                  el("span", "lr-sum", `${oneLine(item.summary) || item.status}${item.duration != null ? ` · ${formatElapsed(item.duration * 1000)}` : ""}`));
        list.append(li);
      }
      details.append(list);
      card.append(details);
    }
    const actions = el("div", "run-actions");
    const view = el("button", "btn btn-sm", `View jobs (${run.job_count || 0})`);
    view.type = "button";
    view.disabled = !run.job_count;
    view.title = run.job_count ? "Open the shortlist with only the jobs this run found." : "This run did not record any jobs.";
    view.addEventListener("click", () => { jobsRun = run.run_id; openJobs(); });
    actions.append(view);
    card.append(actions);
    return card;
  }

  function drawHistory() {
    const panel = $("#panel-history");
    const shown = historyProfile === null ? currentProfile : historyProfile;
    panel.replaceChildren(el("h3", null, "Run history"),
      el("p", "hint", "Every run is kept with its date, profile and resume. Open one to see the jobs it found."));
    if (historyProfiles.length > 1) {
      const field = el("div", "field");
      field.append(el("label", null, "Profile"));
      const select = el("select");
      select.setAttribute("aria-label", "Filter runs by profile");
      [{ key: "", name: "All profiles" }, ...historyProfiles].forEach(({ key, name }) => {
        const option = el("option", null, key && key === currentProfile ? `${name} (current)` : name);
        option.value = key; option.selected = key === shown;
        select.append(option);
      });
      select.addEventListener("change", () => { historyProfile = select.value; drawHistory(); });
      field.append(select);
      panel.append(field);
    }
    const runs = historyRuns.filter((r) => !shown || r.candidate_key === shown);
    if (!runs.length) {
      const empty = el("div", "empty-state");
      empty.append(el("strong", null, "No runs yet"), el("span", null, "Press Run all phases. Each run is saved here with its date and profile."));
      panel.append(empty);
      return;
    }
    let day = "";
    for (const run of runs) {
      const label = new Date(run.started_at).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric" });
      if (label !== day) { day = label; panel.append(el("h4", null, label)); }
      panel.append(runCard(run));
    }
  }

  async function renderHistory() {
    const panel = $("#panel-history");
    panel.replaceChildren(el("p", "hint", "Loading history…"));
    try {
      const data = await api("/api/runs");
      historyRuns = data.runs || [];
      historyProfiles = data.profiles || [];
      currentProfile = data.current_profile || "";
      drawHistory();
    } catch (error) {
      panel.replaceChildren(el("div", "callout err", `Could not load run history: ${error.message}`));
    }
  }

  // Fill the shortlist's run picker: grouped by profile, newest first.
  async function populateRunSelect() {
    const select = $("#jobs-run");
    let runs = [];
    try {
      const data = await api("/api/runs");
      currentProfile = data.current_profile || currentProfile;
      // Another candidate's runs are not offered here; they stay available in History.
      runs = (data.runs || []).filter((run) => !currentProfile || run.candidate_key === currentProfile);
    } catch { /* the list still works without it */ }
    select.replaceChildren();
    const all = el("option", null, "All saved jobs"); all.value = ""; select.append(all);
    const byProfile = new Map();
    for (const run of runs) {
      const key = run.candidate_name || "Unknown profile";
      if (!byProfile.has(key)) byProfile.set(key, []);
      byProfile.get(key).push(run);
    }
    for (const [profile, items] of byProfile) {
      const group = el("optgroup"); group.label = profile;
      for (const run of items) {
        const option = el("option", null, `${runLabel(run)} · ${run.job_count || 0} jobs${run.dry_run ? " · dry run" : ""}`);
        option.value = run.run_id;
        group.append(option);
      }
      select.append(group);
    }
    select.value = jobsRun;
    if (select.value !== jobsRun) { jobsRun = ""; select.value = ""; }
    $("#jobs-current").disabled = !!jobsRun;
  }

  /* ---------------- Run options ---------------- */

  const runOptions = (() => {
    const defaults = { tailorAll: true, scoreLimit: 0 };
    try { return { ...defaults, ...JSON.parse(localStorage.getItem("jobAgentRunOptions") || "{}") }; }
    catch { return defaults; }
  })();
  function saveRunOptions() {
    try { localStorage.setItem("jobAgentRunOptions", JSON.stringify(runOptions)); } catch { /* private mode */ }
  }

  function runOptionsSection(panel) {
    panel.append(el("h4", null, "Run options"));
    panel.append(el("div", "hint", "How a run spends your Groq tokens and what it prepares. Saved in this browser."));

    const limitField = el("div", "field");
    limitField.append(el("label", null, "Score at most"));
    const limit = el("select");
    limit.setAttribute("aria-label", "Maximum number of jobs to score");
    [[0, "Every job (slowest)"], [30, "30 best matches"], [60, "60 best matches"], [100, "100 best matches"]].forEach(([value, label]) => {
      const option = el("option", null, label); option.value = String(value); option.selected = Number(runOptions.scoreLimit) === value;
      limit.append(option);
    });
    limit.addEventListener("change", () => { runOptions.scoreLimit = Number(limit.value); saveRunOptions(); });
    limitField.append(limit, el("div", "hint",
      "Scoring is limited by Groq's tokens-per-minute cap (about 2 jobs a minute on the free plan). Jobs are scored best-match first, so a limit skips only the weakest."));
    panel.append(limitField);

    const all = el("label", "check");
    const box = el("input"); box.type = "checkbox"; box.checked = !!runOptions.tailorAll;
    box.addEventListener("change", () => { runOptions.tailorAll = box.checked; saveRunOptions(); });
    all.append(box, el("span", null, "Make a resume for every job found"));
    const allField = el("div", "field");
    allField.append(all, el("div", "hint",
      "On: weak matches and listings with no description get a resume too, so you can apply to any job. Off: only jobs that qualified. Jobs that did not qualify are never applied to automatically."));
    panel.append(allField);
  }

  function init() {
    hydrateIcons();
    initPalette();
    $("#jobs-btn").addEventListener("click", openJobs);
    $("#jobs-close").addEventListener("click", () => $("#jobs-dialog").close());
    const exportMenu = $("#export-menu");
    exportMenu.addEventListener("click", (e) => {
      if (e.target.closest(".export-item")) setTimeout(() => { exportMenu.open = false; }, 0);
    });
    document.addEventListener("click", (e) => { if (!exportMenu.contains(e.target)) exportMenu.open = false; });
    exportMenu.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && exportMenu.open) { e.stopPropagation(); e.preventDefault(); exportMenu.open = false; exportMenu.querySelector("summary").focus(); }
    });
    $("#jobs-run").addEventListener("change", () => {
      jobsRun = $("#jobs-run").value;
      $("#jobs-current").disabled = !!jobsRun;
      openJobs();
    });
    for (const id of ["#jobs-query", "#jobs-mode", "#jobs-email", "#jobs-current", "#jobs-ready", "#jobs-skipped"]) $(id).addEventListener("input", () => { visibleJobs = 50; renderJobRows(); });
    $("#jobs-more").addEventListener("click", () => { visibleJobs += 50; renderJobRows(); });
    $("#jobs-bundle").addEventListener("click", async () => {
      const button = $("#jobs-bundle"); button.disabled = true; button.textContent = "Preparing download…";
      try {
        const result = await api("/api/export/bundle", {method: "POST", body: "{}"});
        const link = document.createElement("a"); link.href = `/api/file?path=${encodeURIComponent(result.path)}`;
        link.download = "application_pack.zip"; document.body.append(link); link.click(); link.remove();
        if ($("#jobs-dialog").open) await openJobs();
      } catch (error) { toast(error.message, "err"); }
      finally { button.disabled = false; button.textContent = "Download everything (ZIP)"; }
    });
    const tabButtons = [...document.querySelectorAll(".tab")];
    tabButtons.forEach((tab, i) => {
      tab.addEventListener("click", () => switchTab(tab.dataset.tab));
      // Arrow keys move between tabs, as the tablist pattern expects.
      tab.addEventListener("keydown", (e) => {
        const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
        if (!step) return;
        const next = tabButtons[(i + step + tabButtons.length) % tabButtons.length];
        switchTab(next.dataset.tab);
        next.focus();
      });
    });
    switchTab("details");

    $("#log-search").addEventListener("input", (e) => { logFilter.query = e.target.value; renderLog(); });
    document.querySelectorAll(".log-level").forEach((button) => {
      button.addEventListener("click", () => {
        logFilter.level = button.dataset.level;
        document.querySelectorAll(".log-level").forEach((b) => b.classList.toggle("is-active", b === button));
        renderLog();
      });
    });
    $("#log-jump").addEventListener("click", () => {
      const body = $("#log-body");
      body.scrollTop = body.scrollHeight;
      $("#log-jump").hidden = true;
    });
    $("#log-clear").addEventListener("click", () => {
      logLines.length = 0;
      renderLog();
    });
    $("#log-copy").addEventListener("click", async () => {
      const text = filteredLogLines()
        .map((entry) => `${fmtTime(entry.ts)}${entry.phase ? ` [${entry.phase}]` : ""} ${entry.line}`)
        .join("\n");
      if (!text) { toast("Nothing to copy."); return; }
      try {
        await navigator.clipboard.writeText(text);
        toast("Log copied to clipboard.", "ok");
      } catch {
        toast("Couldn't access the clipboard.", "err");
      }
    });

    $("#run-btn").addEventListener("click", () => {
      startRun(["intake", "source", "evaluate", "tailor", "apply", "track", "prep"]);
    });

    $("#cancel-btn").addEventListener("click", async () => {
      try { await api("/api/cancel", { method: "POST" }); toast("Stopping after the current phase."); }
      catch (err) { toast(err.message, "err"); }
    });

    $("#dry-run").addEventListener("change", render);

    $("#fit-btn").addEventListener("click", () => {
      fitMode = !fitMode;
      try { localStorage.setItem("flowFit", fitMode ? "1" : "0"); } catch { /* private mode */ }
      renderNodes();
      requestAnimationFrame(renderEdges);
    });

    // Re-fit when the window (or the inspector beside the canvas) changes size.
    // Only reacts to a real size change (a scrollbar appearing or the layout
    // swapping must not retrigger a re-fit), so it cannot feed back on itself.
    let resizeTimer = null, lastSize = "";
    new ResizeObserver(() => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        const canvas = $(".canvas");
        const size = `${canvas.clientWidth}x${canvas.clientHeight}`;
        if (!state || size === lastSize) return;
        lastSize = size;
        renderNodes();
        requestAnimationFrame(renderEdges);
      }, 120);
    }).observe($(".canvas"));

    $("#theme-btn").addEventListener("click", () => {
      const root = document.documentElement;
      const next = root.dataset.theme === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      try { localStorage.setItem("job-agent-theme", next); } catch { /* private mode */ }
    });

    try {
      const saved = localStorage.getItem("job-agent-theme");
      if (saved) document.documentElement.dataset.theme = saved;
    } catch { /* private mode */ }

    $("#banner-setup").addEventListener("click", () => openSetup(0));
    $("#banner-dismiss").addEventListener("click", () => { bannerDismissed = true; renderBanner(); });
    $("#run-banner-dismiss").addEventListener("click", () => { runBannerDismissed = true; renderRunBanner(); });
    $("#run-banner-fresh").addEventListener("click", async () => {
      if (!confirm("Move the previous run's results to history? Seen jobs, the jobs CSV and the outreach log are kept, so nothing will be repeated.")) return;
      try {
        const result = await api("/api/outputs/archive", { method: "POST", body: "{}" });
        state = result.state;
        runBannerDismissed = true;
        render();
        toast("Previous results moved to history. Run all phases to search again.", "ok");
      } catch (err) {
        toast(err.message, "err");
      }
    });

    refreshState().then(() => {
      // Open the wizard unprompted when there is no usable profile: without one
      // every phase downstream is either blocked or would run as the demo
      // candidate, which is exactly the mistake this is here to prevent.
      if (state?.setup && (state.setup.needs_profile || state.setup.using_sample)) {
        openSetup(0);
      }
      connectEvents();
      startStatePolling();
    });
    // A CLI run uses the same artifacts but has no dashboard SSE connection.
    setInterval(() => { if (!document.hidden) refreshState(); }, 10000);
    setInterval(() => { if (!document.hidden) tickRunningTimer(); }, 1000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
