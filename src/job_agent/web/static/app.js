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

  // Positions are fixed rather than auto-laid-out: the pipeline is a known,
  // unchanging shape, and a stable layout is easier to learn than a solver's.
  // Serpentine layout: phases 1-3 run left to right, then the flow drops a row
  // and phases 4-6 run right to left. This keeps all six phases on screen at
  // once — a single row would be 1630px wide and force horizontal scrolling,
  // which defeats the point of seeing the whole pipeline at a glance.
  const NODE_POS = {
    resume:   { x: 258, y: 16,  kind: "input" },
    settings: { x: 498, y: 16,  kind: "input" },
    intake:   { x: 258, y: 150 },
    source:   { x: 498, y: 150 },
    evaluate: { x: 738, y: 150 },
    tailor:   { x: 738, y: 420 },
    apply:    { x: 498, y: 420 },
    track:    { x: 258, y: 420 },
    prep:     { x: 258, y: 690 },
  };

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
    document.querySelectorAll(".node[data-node]").forEach((node) => {
      const box = node.getBoundingClientRect();
      rects[node.dataset.node] = {
        left: box.left - flow.left,
        right: box.right - flow.left,
        top: box.top - flow.top,
        bottom: box.bottom - flow.top,
        cx: box.left - flow.left + box.width / 2,
        cy: box.top - flow.top + box.height / 2,
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

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  };

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

  function connectEvents() {
    const source = new EventSource("/api/events");

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
    switch (evt.type) {
      case "run_start":
        ranThisSession = true;
        running = true;
        logLines.length = 0;
        pushLog("evt", `Run started: ${evt.phases.join(" → ")}`);
        render();
        break;

      case "phase_start":
        runStatus[evt.phase] = "running";
        delete runSummary[evt.phase];
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
        runStatus[evt.phase] = evt.status === "warning" ? "warning" : ok ? "ready" : evt.status === "cancelled" ? "halted" : "error";
        runSummary[evt.phase] = evt.summary || {};
        if (evt.summary && evt.summary.halt_reason && evt.status !== "error") runStatus[evt.phase] = "halted";

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
        pushLog("evt", `Run ${evt.status}.`);
        toast(
          evt.status === "ok" ? "Run finished." : `Run ${evt.status}.`,
          evt.status === "ok" ? "ok" : "err"
        );
        refreshState();
        if ($("#jobs-dialog").open) openJobs();
        break;
    }
  }

  function pushLog(kind, line, phase) {
    logLines.push({ kind, line, phase });
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
    $("#run-banner-text").textContent =
      `These are results from your previous run (${when.toLocaleString()}, ${age}), not a new search. ` +
      "Press Run all phases to refresh the search. Previously seen listings can appear in the latest CSV; processed applications are not repeated. " +
      "Start fresh clears this view first.";
  }

  function render() {
    renderBanner();
    renderRunBanner();
    renderPills();
    renderNodes();
    // Deferred a frame: edge anchors are measured from the DOM, which only has
    // real geometry once the browser has laid the new nodes out.
    requestAnimationFrame(renderEdges);
    renderPanel();
    $("#run-btn").disabled = running;
    $("#cancel-btn").disabled = !running;
    $("#run-btn").textContent = running ? "Running…" : "▶ Run all phases";

    const dry = $("#dry-run").checked;
    $("#dry-toggle").classList.toggle("is-live", !dry);
    $("#dry-label").textContent = dry
      ? "Dry run (nothing is submitted)"
      : "LIVE — will submit real applications";
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
    flow.querySelectorAll(".node").forEach((n) => n.remove());
    if (!state) return;

    // Input nodes describe what you feed the pipeline.
    flow.append(inputNode("resume", "Resume PDF", resumeLabel(), "📄"));
    flow.append(inputNode("settings", "Search Settings", settingsLabel(), "⚙️"));

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

  function inputNode(id, label, sub, icon) {
    const pos = NODE_POS[id];
    const node = el("button", `node node-input status-${state?.config?.status === "error" && id === "settings" ? "error" : "ready"}`);
    node.style.left = `${pos.x}px`;
    node.style.top = `${pos.y}px`;
    node.dataset.node = id;

    const head = el("div", "node-head");
    head.append(el("div", "node-index", icon), el("div", "node-title", label));
    node.append(head, el("div", "node-sub", sub));

    node.addEventListener("click", () => { selected = id; switchTab("settings"); render(); });
    return node;
  }

  function phaseNode(id, index) {
    const phase = state.phases[id];
    const status = nodeStatus(id);
    const pos = NODE_POS[id];

    const node = el("button", `node status-${status}${selected === id ? " is-selected" : ""}`);
    node.style.left = `${pos.x}px`;
    node.style.top = `${pos.y}px`;
    node.dataset.node = id;

    const head = el("div", "node-head");
    head.append(el("div", "node-index", String(index)), el("div", "node-title", phase.title));
    node.append(head, el("div", "node-sub", phase.subtitle));

    const badge = el("div", "node-status");
    if (status === "running") badge.append(el("span", "spin"));
    badge.append(el("span", null, STATUS_LABEL[status] || status));
    node.append(badge);

    node.append(el("div", "node-summary", phase.summary));

    const metrics = el("div", "node-metrics");
    Object.entries(phase.metrics || {}).slice(0, 3).forEach(([key, value]) => {
      const chip = el("span", "metric");
      chip.append(document.createTextNode(`${key} `), el("b", null, String(value)));
      metrics.append(chip);
    });
    if (metrics.children.length) node.append(metrics);

    const run = el("button", "btn btn-sm node-run", `Run ${phase.title.toLowerCase()}`);
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
      tab.classList.toggle("is-active", tab.dataset.tab === name);
    });
    $("#panel-details").hidden = name !== "details";
    $("#panel-log").hidden = name !== "log";
    $("#panel-settings").hidden = name !== "settings";
    $("#panel-analytics").hidden = name !== "analytics";
    if (name === "analytics") renderAnalytics();
    if (name === "settings") renderSettings();
  }

  async function renderAnalytics() {
    const panel = $("#panel-analytics"); panel.replaceChildren(el("p", null, "Loading outcomes…"));
    try {
      const result = await api("/api/analytics"); panel.replaceChildren(el("h3", null, "Application outcomes"), el("p", null, result.note));
      for (const stage of result.funnel) {
        const row = el("div", "analytics-row");
        row.append(el("strong", null, `${stage.stage}: ${stage.count}`), el("p", "hint", stage.conversion == null ? "Conversion unknown" : `${(stage.conversion * 100).toFixed(1)}% of preceding stage`));
        const meter = el("meter"); meter.min = 0; meter.max = Math.max(1, result.funnel[0].count); meter.value = stage.count; meter.title = `${stage.stage}: ${stage.count}`;
        row.append(meter); panel.append(row);
      }
      panel.append(el("p", null, result.median_response_days == null ? "Median response time: unknown" : `Median response time: ${result.median_response_days.toFixed(1)} days (${result.timed_responses} dated replies)`));
      for (const [key, title] of [["by_score", "By fit score"], ["by_source", "By source"], ["by_variant", "By resume format"], ["by_role", "By role"]]) {
        panel.append(el("h4", null, title));
        for (const row of result[key]) panel.append(el("p", null, `${row.label}: ${row.replied}/${row.applied} replies${result.reply_tracking ? ` (${(row.response_rate * 100).toFixed(1)}%)` : " (tracking unavailable)"}`));
      }
    } catch (error) { panel.replaceChildren(el("p", null, error.message)); }
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

  function renderLog() {
    const panel = $("#panel-log");
    const atBottom = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 60;
    panel.innerHTML = "";

    if (!logLines.length) {
      panel.append(el("span", "log-empty", "No activity yet. Press “Run all phases” to start."));
      return;
    }
    for (const entry of logLines) {
      const line = el("div", `log-line ${entry.kind}`);
      if (entry.phase) line.append(el("span", "tag", `[${entry.phase}] `));
      line.append(document.createTextNode(entry.line));
      panel.append(line);
    }
    // Only auto-scroll when the user was already following the tail.
    if (atBottom) panel.scrollTop = panel.scrollHeight;
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

    /* --- Where things land --- */
    panel.append(el("h4", null, "Output locations"));
    const dl = el("dl", "kv");
    dl.append(el("dt", null, "Artifacts"), el("dd", null, state.paths.outputs));
    dl.append(el("dt", null, "Resumes"), el("dd", null, state.paths.resumes));
    dl.append(el("dt", null, "Tracker"), el("dd", null, state.paths.tracker));
    dl.append(el("dt", null, "Jobs CSV"), el("dd", null, state.paths.jobs_csv));
    panel.append(dl);
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
  function renderJobRows() {
    const query = $("#jobs-query").value.toLowerCase();
    const mode = $("#jobs-mode").value;
    const hiring = $("#jobs-email").checked;
    const current = $("#jobs-current").checked;
    const ready = $("#jobs-ready").checked;
    const rows = jobRows.filter(row => (!mode || row["Work Mode"] === mode)
      && (!current || row["Search Batch"] === "Current search")
      && (!ready || row["Application Readiness"] === "Ready for your review")
      && (!hiring || ["hiring", "person"].includes(row["Email Type"]))
      && ["Title", "Company", "Location"].some(key => (row[key] || "").toLowerCase().includes(query)))
      .sort((a, b) => (Number(b["Fit Score"]) || 0) - (Number(a["Fit Score"]) || 0));
    $("#jobs-count").textContent = `${rows.length} matching jobs · ${jobRows.length} saved in total · showing ${Math.min(visibleJobs, rows.length)} · scores out of 10`;
    $("#jobs-more").hidden = rows.length <= visibleJobs;
    const body = $("#jobs-rows"); body.replaceChildren();
    for (const row of rows.slice(0, visibleJobs)) {
      const tr = el("tr");
      const role = el("td"); role.append(el("strong", null, row.Title), el("p", null, row.Company), el("small", null, `${row.Source || ""} · ${row.Status || "found"}`));
      if (row["Search Batch"] !== "Current search") role.append(el("p", "hint", "Earlier search; fit may be stale"));
      const scoreDetails = el("details"); scoreDetails.append(el("summary", null, "Why this score"));
      scoreDetails.append(el("p", null, row["Score Reasoning"] || "No scoring explanation is recorded."));
      scoreDetails.append(el("p", "hint", `Matched: ${row["Matched Skills"] || "Not recorded"}`));
      scoreDetails.append(el("p", "hint", `Reported gaps: ${row["Missing Skills"] || "None recorded"}`));
      if (row["Skills Found In Profile"]) scoreDetails.append(el("p", "hint", `Already in your profile: ${row["Skills Found In Profile"]}. Review the scorer's gap assessment; the score has not been changed.`));
      scoreDetails.append(el("p", "hint", "Only add keywords supported by your actual experience.")); role.append(scoreDetails);
      const location = el("td"); location.append(el("div", null, `${row.Location || "Unknown"} · ${row["Work Mode"] || ""}`), el("p", "hint", row["Remote Eligibility"] || "Review eligibility"));
      location.title = row["Eligibility Notes"] || "";
      const contact = el("td"); contact.append(el("div", null, row["HR / Careers Email"] || "No published email found"), el("p", "hint", row["Email Verification"] || "Deliverability not checked"));
      if (row["Email Found On"]) contact.append(safeLink("Contact source", row["Email Found On"]));
      if (row["Possible Contacts"]) { const leads = el("details"); leads.append(el("summary", null, "Possible contacts (unverified)"), el("p", null, row["Possible Contacts"])); contact.append(leads); }
      const actions = el("td");
      actions.append(safeLink("View job / apply", row["Apply URL"] || row["Job URL"]));
      if (row["Tailored Resume Path"]) actions.append(el("br"), safeLink("Open PDF", `/api/file?path=${encodeURIComponent(row["Tailored Resume Path"])}`));
      if (row["Tailored Resume Path"]) actions.append(el("br"), safeLink("Download resume", `/api/file?download=1&path=${encodeURIComponent(row["Tailored Resume Path"])}`));
      actions.append(el("p", "hint", row["Resume Check"] || "Resume not yet generated"));
      actions.append(el("p", "hint", row["Next Step"] || "Review the employer listing before applying."));
      for (const column of ["Interview Prep", "Cover Letter"]) if (row[column]) actions.append(el("br"), safeLink(column, `/api/file?download=1&path=${encodeURIComponent(row[column])}`));
      if (row["Tailored Resume Path"] && !row["Interview Prep"]) {
        const prep = el("button", "btn btn-sm", "Prepare interview guide"); prep.disabled = running;
        prep.addEventListener("click", () => { $("#jobs-dialog").close(); startRun(["prep"], row["Job ID"]); }); actions.append(prep);
      }
      if (!row.Status.startsWith("replied_") && (row.Status !== "applied" || manuallyApplied.has(row["Job ID"]))) {
        const undo = manuallyApplied.has(row["Job ID"]);
        const mark = el("button", "btn btn-sm", undo ? "Undo applied marker" : "Mark as applied");
        mark.addEventListener("click", async () => {
          mark.disabled = true;
          try {
            const result = await api("/api/jobs/applied", {method: "POST", body: JSON.stringify({job_id: row["Job ID"], undo})});
            toast((result.warnings || []).join(" ") || (undo ? "Manual marker undone." : "Recorded. No application was sent by this action."));
            await openJobs();
          } catch (error) { toast(error.message, "err"); mark.disabled = false; }
        });
        actions.append(mark);
      }
      if (row["Email Draft File"] && outreachDirectory) actions.append(safeLink("Open unsent email draft", `/api/file?path=${encodeURIComponent(outreachDirectory + "/" + row["Email Draft File"])}`));
      tr.append(role, location, el("td", null, row["Fit Score"] || "Not scored"), contact, actions); body.append(tr);
    }
    if (!rows.length) { const tr = el("tr"); const td = el("td", null, "No jobs match. Run sourcing or adjust your filters."); td.colSpan = 5; tr.append(td); body.append(tr); }
  }

  async function openJobs() {
    const dialog = $("#jobs-dialog"); if (!dialog.open) dialog.showModal();
    $("#jobs-count").textContent = "Loading jobs…";
    try {
      const result = await api("/api/jobs"); jobRows = result.jobs || []; visibleJobs = 50;
      outreachDirectory = result.outreach_dir || "";
      manuallyApplied = new Set(result.manually_applied || []);
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
    } catch (error) { $("#jobs-count").textContent = error.message; }
  }

  function init() {
    $("#jobs-btn").addEventListener("click", openJobs);
    $("#jobs-close").addEventListener("click", () => $("#jobs-dialog").close());
    for (const id of ["#jobs-query", "#jobs-mode", "#jobs-email", "#jobs-current", "#jobs-ready"]) $(id).addEventListener("input", () => { visibleJobs = 50; renderJobRows(); });
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
    document.querySelectorAll(".tab").forEach((tab) => {
      tab.addEventListener("click", () => switchTab(tab.dataset.tab));
    });

    $("#run-btn").addEventListener("click", () => {
      startRun(["intake", "source", "evaluate", "tailor", "apply", "track", "prep"]);
    });

    $("#cancel-btn").addEventListener("click", async () => {
      try { await api("/api/cancel", { method: "POST" }); toast("Stopping after the current phase."); }
      catch (err) { toast(err.message, "err"); }
    });

    $("#dry-run").addEventListener("change", render);

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
    });
    // A CLI run uses the same artifacts but has no dashboard SSE connection.
    setInterval(() => { if (!document.hidden) refreshState(); }, 10000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
