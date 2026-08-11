const TOKEN = document.querySelector('meta[name="dashboard-token"]').content;
const ALL_KVMS = "__all__";

const el = (id) => document.getElementById(id);

const state = {
  section: "overview",
  range: 7,
  kvms: [],
  fleetHistory: null,
  activityHistory: null,
  schedule: null,
  activityKvm: null,
  busy: new Set(),
  triage: null,
  stream: null,
};

/* ---------------- transport ---------------- */

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "X-Dashboard-Token": TOKEN,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    let detail = `Request failed with status ${response.status}.`;
    try {
      const payload = await response.json();
      if (payload && typeof payload.detail === "string") detail = payload.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return response;
}

const getJSON = (path) => api(path).then((response) => response.json());

/* ---------------- DOM helpers ---------------- */

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function clear(target) {
  while (target.firstChild) target.removeChild(target.firstChild);
}

function chip(kind, glyphOrDot, label, { large = false } = {}) {
  const element = node("span", `chip chip--${kind}${large ? " chip--lg" : ""}`);
  element.append(
    glyphOrDot === null ? node("span", "chip__dot") : node("span", "chip__glyph", glyphOrDot),
  );
  element.append(node("span", "chip__name", label));
  return element;
}

function availabilityChip(value, options) {
  if (value === "active") return chip("active", null, "Active", options);
  if (value === "away") return chip("away", null, "Away", options);
  return chip("unknown", null, "Unknown", options);
}

function outcomeChip(outcome) {
  return outcome === "success"
    ? chip("success", "✓", "Success")
    : chip("failure", "✕", "Stopped");
}

function relativeTime(iso) {
  if (!iso) return "never";
  const seconds = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (!Number.isFinite(seconds)) return "never";
  if (seconds < 60) return "just now";
  const units = [
    ["minute", 60],
    ["hour", 3600],
    ["day", 86400],
    ["week", 604800],
  ];
  let label = "minute";
  let size = 60;
  for (const [name, span] of units) {
    if (seconds >= span) {
      label = name;
      size = span;
    }
  }
  const count = Math.floor(seconds / size);
  return `${count} ${label}${count === 1 ? "" : "s"} ago`;
}

function localTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function detailText(record) {
  if (record.error) return record.error;
  if (record.changed === true) return "State changed and visually verified";
  if (record.changed === false && record.desired) return "Already correct; no click sent";
  return "Verified from the visible toggle";
}

/* ---------------- tooltip ---------------- */

const tooltip = el("tooltip");

function bindTooltip(target, value, label) {
  const place = () => {
    const box = target.getBoundingClientRect();
    const own = tooltip.getBoundingClientRect();
    const left = Math.min(Math.max(8, box.left), window.innerWidth - own.width - 8);
    const above = box.top - own.height - 10;
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${above < 8 ? box.bottom + 10 : above}px`;
  };
  const show = () => {
    clear(tooltip);
    tooltip.append(node("div", "tooltip__value", value));
    if (label) tooltip.append(node("div", "tooltip__label", label));
    tooltip.dataset.open = "true";
    tooltip.setAttribute("aria-hidden", "false");
    place();
  };
  const hide = () => {
    tooltip.dataset.open = "false";
    tooltip.setAttribute("aria-hidden", "true");
  };
  target.addEventListener("pointerenter", show);
  target.addEventListener("pointermove", place);
  target.addEventListener("pointerleave", hide);
  target.addEventListener("focus", show);
  target.addEventListener("blur", hide);
}

/* ---------------- fleet rail ---------------- */

function outcomeFor(kvm) {
  const summary = state.fleetHistory?.summary;
  return summary?.per_kvm.find((item) => item.kvm === kvm) || null;
}

function renderFleet() {
  const holder = el("fleet");
  clear(holder);
  const required = state.schedule?.desired_now || null;
  el("fleet-hint").textContent = state.kvms.length
    ? `${state.kvms.length} configured · processed one at a time`
    : "No names in PIKVM_PROFILES";

  if (!state.kvms.length) {
    holder.append(node("p", "empty", "Add profile names to PIKVM_PROFILES to control a KVM."));
    return;
  }

  for (const profile of state.kvms) {
    const outcome = outcomeFor(profile.name);
    const observed = state.schedule?.applied?.[profile.name] || outcome?.last_observed || null;
    const busy = state.busy.has(profile.name);
    const drifted = Boolean(required && observed && observed !== required);

    const card = node("article", "kvmcard");
    if (busy) card.classList.add("is-busy");
    else if (drifted) card.classList.add("is-drifted");

    const top = node("div", "kvmcard__top");
    const identity = node("div");
    identity.append(node("div", "kvmcard__name", profile.name));
    identity.append(node("div", "kvmcard__host", profile.endpoint || "endpoint unavailable"));
    top.append(identity);

    const stateBox = node("div", "kvmcard__state");
    stateBox.append(availabilityChip(observed, { large: true }));
    if (!profile.configured) {
      stateBox.append(chip("failure", "✕", "Unconfigured"));
    } else if (busy) {
      stateBox.append(chip("muted", "▸", "Running"));
    } else if (drifted) {
      stateBox.append(chip("warn", "!", `Wants ${required}`));
    } else if (required && observed) {
      stateBox.append(chip("success", "✓", "Matches schedule"));
    }
    top.append(stateBox);
    card.append(top);

    if (outcome) {
      const percent = Math.round(outcome.success_rate * 100);
      const meter = node("div", "kvmcard__meter");
      meter.tabIndex = 0;
      meter.setAttribute("role", "img");
      meter.setAttribute(
        "aria-label",
        `${profile.name}: ${percent}% of ${outcome.total} runs verified`,
      );
      const head = node("div", "kvmcard__meterhead");
      head.append(node("span", null, "Verified runs"));
      const value = node("span");
      value.append(node("b", null, `${percent}%`));
      value.append(document.createTextNode(` of ${outcome.total}`));
      head.append(value);
      meter.append(head);
      const track = node("div", "meter__track");
      const fill = node("div", "meter__fill");
      fill.style.width = `${percent}%`;
      track.append(fill);
      meter.append(track);
      bindTooltip(
        meter,
        `${percent}% verified`,
        `${outcome.success} succeeded · ${outcome.failure} stopped · last run ${relativeTime(outcome.last_at)}`,
      );
      card.append(meter);
    } else {
      card.append(node("div", "kvmcard__note", "No runs in this range."));
    }

    const actions = node("div", "kvmcard__actions");
    for (const [action, label] of [
      ["get", "Check"],
      ["active", "Active"],
      ["away", "Away"],
    ]) {
      const button = node("button", "button button--small", label);
      button.type = "button";
      button.dataset.action = action;
      button.dataset.kvm = profile.name;
      button.disabled = busy || !profile.configured;
      actions.append(button);
    }
    card.append(actions);

    if (outcome) {
      card.append(
        node("div", "kvmcard__note kvmcard__note--last", `Last run ${relativeTime(outcome.last_at)}`),
      );
    }
    holder.append(card);
  }
}

/* ---------------- overview ---------------- */

function renderOverview() {
  const payload = state.fleetHistory;
  if (!payload) return;
  const summary = payload.summary;

  el("m-rate").textContent = summary.total ? `${Math.round(summary.success_rate * 100)}%` : "—";
  el("m-rate-sub").textContent = summary.total
    ? `${summary.success} of ${summary.total} runs`
    : "no runs in range";
  el("m-changed").textContent = summary.total ? String(summary.changes_applied) : "—";
  el("m-noop").textContent = summary.total ? String(summary.no_ops) : "—";
  el("m-reads").textContent = summary.total ? String(summary.reads) : "—";
  el("metrics-scope").textContent = summary.first_at
    ? `all environments · ${localTime(summary.first_at)} → ${localTime(summary.last_at)}`
    : "all environments";

  renderBars(el("cats"), summary.failure_categories, (item) => item.label, "stops recorded");
  renderCountTable(el("cats-table"), summary.failure_categories, "Category", (item) => item.label);

  const body = el("recent-body");
  clear(body);
  const recent = payload.records.slice(0, 8);
  if (!recent.length) {
    const row = node("tr");
    const cell = node("td", "table__detail", "No runs recorded in this range.");
    cell.colSpan = 6;
    row.append(cell);
    body.append(row);
    return;
  }
  for (const record of recent) body.append(historyRow(record));
}

function renderBars(holder, items, labelOf, emptyNoun) {
  clear(holder);
  if (!items.length) {
    holder.append(node("p", "empty", `No ${emptyNoun} in this range.`));
    return;
  }
  const max = Math.max(...items.map((item) => item.count));
  for (const item of items) {
    const label = labelOf(item);
    const bar = node("div", "bar");
    bar.tabIndex = 0;
    bar.setAttribute("role", "img");
    bar.setAttribute("aria-label", `${item.count} runs: ${label}`);
    bar.append(node("div", "bar__label", label));
    const plot = node("div", "bar__plot");
    const fill = node("div", "bar__fill");
    fill.style.width = `${Math.max(2, (item.count / max) * 100)}%`;
    plot.append(fill);
    plot.append(node("span", "bar__value", item.count));
    bar.append(plot);
    bindTooltip(bar, `${item.count} run${item.count === 1 ? "" : "s"}`, label);
    holder.append(bar);
  }
}

function renderCountTable(holder, items, heading, labelOf) {
  clear(holder);
  const table = node("table", "table");
  const head = node("thead");
  const headRow = node("tr");
  for (const text of ["Runs", heading]) {
    const cell = node("th", null, text);
    cell.scope = "col";
    headRow.append(cell);
  }
  head.append(headRow);
  table.append(head);
  const body = node("tbody");
  for (const item of items) {
    const row = node("tr");
    row.append(node("td", null, item.count));
    row.append(node("td", "table__detail", labelOf(item)));
    body.append(row);
  }
  table.append(body);
  holder.append(table);
}

/* ---------------- availability section ---------------- */

function renderAvailability() {
  const body = el("avail-body");
  clear(body);
  const required = state.schedule?.desired_now || null;

  for (const profile of state.kvms) {
    const outcome = outcomeFor(profile.name);
    const verified = state.schedule?.applied?.[profile.name] || null;
    const busy = state.busy.has(profile.name);

    const row = node("tr");
    row.append(node("td", "table__kvm", profile.name));

    const observedCell = node("td");
    observedCell.append(availabilityChip(outcome?.last_observed || verified));
    row.append(observedCell);

    const wantedCell = node("td");
    wantedCell.append(availabilityChip(required));
    row.append(wantedCell);

    const verifiedCell = node("td", "table__detail");
    verifiedCell.textContent = verified
      ? `${verified} · ${relativeTime(state.schedule.applied_updated_at)}`
      : "not recorded";
    row.append(verifiedCell);

    const actionCell = node("td", "table__actions");
    for (const [action, label] of [
      ["get", "Check"],
      ["active", "Active"],
      ["away", "Away"],
    ]) {
      const button = node("button", "button button--small", label);
      button.type = "button";
      button.dataset.action = action;
      button.dataset.kvm = profile.name;
      button.disabled = busy || !profile.configured;
      actionCell.append(button);
    }
    row.append(actionCell);
    body.append(row);
  }
}

/* ---------------- schedule section ---------------- */

function renderSchedule() {
  const snapshot = state.schedule;
  if (!snapshot) return;

  const pill = el("schedule-pill");
  pill.className = `pill pill--${snapshot.healthy ? "good" : "critical"}`;
  clear(pill);
  pill.append(node("span", "pill__icon", snapshot.healthy ? "✓" : "✕"));
  pill.append(
    node(
      "span",
      "pill__text",
      snapshot.healthy ? "Schedule healthy" : `Schedule broken (${snapshot.problems.length})`,
    ),
  );

  const required = el("required-pill");
  required.className = "pill pill--series";
  clear(required);
  required.append(node("span", "pill__icon", "●"));
  required.append(
    node("span", "pill__text", `Slack should be ${snapshot.desired_now} now`),
  );

  const problems = el("schedule-problems");
  clear(problems);
  problems.hidden = !snapshot.problems.length;
  if (snapshot.problems.length) {
    const title = node("div", "problems__title");
    title.append(node("span", null, "✕"));
    title.append(node("span", null, "Scheduled runs are not working"));
    problems.append(title);
    const list = node("ul");
    for (const problem of snapshot.problems) list.append(node("li", null, problem));
    problems.append(list);
  }

  const desired = el("sched-desired");
  clear(desired);
  desired.append(availabilityChip(snapshot.desired_now));

  el("sched-next").textContent =
    `${snapshot.next_transition_to === "active" ? "Active" : "Away"} · ` +
    new Date(snapshot.next_transition_at).toLocaleString(undefined, {
      weekday: "short",
      hour: "2-digit",
      minute: "2-digit",
    });

  const interpStatus = el("sched-interp-status");
  clear(interpStatus);
  if (!snapshot.interpreter) {
    interpStatus.append(chip("muted", "•", "Not installed"));
  } else {
    interpStatus.append(
      snapshot.interpreter_can_run
        ? chip("success", "✓", "Runnable")
        : chip("failure", "✕", "Cannot import work_agent"),
    );
  }
  el("sched-interp").textContent = snapshot.interpreter || "";

  const agents = el("agent-list");
  clear(agents);
  if (!snapshot.agents.length) agents.append(node("li", null, "No LaunchAgents generated."));
  for (const agent of snapshot.agents) {
    const item = node("li");
    item.append(node("span", "rowlist__name", agent.short_label));
    const meta = node("span", "rowlist__meta");
    meta.textContent = `installed ${agent.installed ? "yes" : "no"} · loaded ${agent.loaded ? "yes" : "no"}`;
    item.append(meta);
    agents.append(item);
  }

  const applied = el("applied-list");
  clear(applied);
  const entries = Object.entries(snapshot.applied);
  if (!entries.length) applied.append(node("li", null, "Nothing verified yet."));
  for (const [kvm, value] of entries) {
    const item = node("li");
    item.append(node("span", "rowlist__name", kvm));
    item.append(availabilityChip(value));
    applied.append(item);
  }
  el("applied-when").textContent = snapshot.applied_updated_at
    ? `recorded ${relativeTime(snapshot.applied_updated_at)}`
    : "";
}

/* ---------------- activity section ---------------- */

function historyRow(record) {
  const row = node("tr");
  row.append(node("td", "table__when", localTime(record.timestamp)));
  row.append(node("td", "table__kvm", record.kvm));
  row.append(
    node("td", null, record.desired ? (record.desired === "active" ? "Active" : "Away") : "Read only"),
  );
  const observed = node("td");
  observed.append(availabilityChip(record.observed));
  row.append(observed);
  const outcome = node("td");
  outcome.append(outcomeChip(record.outcome));
  row.append(outcome);
  row.append(node("td", "table__detail", detailText(record)));
  return row;
}

function renderActivity() {
  const payload = state.activityHistory;
  if (!payload) return;

  renderBars(el("reasons"), payload.summary.failure_reasons, (item) => item.reason, "stops recorded");
  renderCountTable(el("reasons-table"), payload.summary.failure_reasons, "Stop reason", (item) => item.reason);

  const filters = el("history-filters");
  clear(filters);
  const options = [{ name: null, label: "All environments" }].concat(
    state.kvms.map((profile) => ({ name: profile.name, label: profile.name })),
  );
  for (const option of options) {
    const button = node("button", "filterchip", option.label);
    button.type = "button";
    if (state.activityKvm === option.name) button.classList.add("is-on");
    button.addEventListener("click", () => {
      state.activityKvm = option.name;
      refresh();
    });
    filters.append(button);
  }

  el("history-hint").textContent = payload.records.length
    ? `showing ${payload.records.length} of ${payload.summary.total}` +
      (payload.unreadable_lines ? ` · ${payload.unreadable_lines} unreadable line(s)` : "")
    : payload.log_present
      ? "nothing in this range"
      : "no operation log yet";

  const body = el("history-body");
  clear(body);
  if (!payload.records.length) {
    const row = node("tr");
    const cell = node("td", "table__detail", "No runs recorded in this range.");
    cell.colSpan = 6;
    row.append(cell);
    body.append(row);
    return;
  }
  for (const record of payload.records) body.append(historyRow(record));
}

/* ---------------- Slack triage ---------------- */

const ATTENTION_LABEL = {
  mentioned: "Mention",
  direct: "Direct message",
  unread: "Unread",
};

function renderTriageButtons() {
  const holder = el("triage-per-kvm");
  clear(holder);
  for (const profile of state.kvms.filter((item) => item.configured)) {
    const button = node("button", "button button--small", profile.name);
    button.type = "button";
    button.dataset.triage = profile.name;
    button.disabled = state.busy.has(profile.name);
    holder.append(button);
  }
}

function renderTriage() {
  const holder = el("triage-body");
  clear(holder);
  const payload = state.triage;
  if (!payload) {
    holder.append(
      node("p", "empty", "No triage run yet. Reading the sidebar never opens a conversation."),
    );
    return;
  }
  for (const report of payload.reports) {
    const card = node("section", "triagecard");
    const head = node("div", "triagecard__head");
    head.append(node("span", "triagecard__kvm", report.kvm));
    if (!report.success) {
      head.append(chip("failure", "✕", report.error || "triage unavailable"));
      card.append(head);
      holder.append(card);
      continue;
    }
    const attention = report.items.filter((item) => item.attention !== "unread");
    head.append(chip("warn", "!", `${attention.length} need attention`));
    head.append(
      node(
        "span",
        "triagecard__meta",
        `${report.items.length} unread · confidence ${Math.round(report.confidence * 100)}%`,
      ),
    );
    card.append(head);

    if (!report.items.length) {
      card.append(node("p", "triagecard__meta", "Nothing unread."));
    } else {
      const table = node("table", "table");
      const thead = node("thead");
      const headRow = node("tr");
      for (const label of ["Conversation", "Kind", "Unread", "Why"]) {
        const cell = node("th", null, label);
        cell.scope = "col";
        headRow.append(cell);
      }
      thead.append(headRow);
      table.append(thead);
      const body = node("tbody");
      for (const item of report.items) {
        const row = node("tr");
        const name = node("td", "table__kvm");
        if (item.has_mention) name.append(node("span", "triagecard__at", "@"));
        name.append(node("span", null, item.name));
        row.append(name);
        row.append(node("td", null, item.kind.replace(/_/g, " ")));
        row.append(node("td", null, item.unread_count || "—"));
        row.append(node("td", "table__detail", ATTENTION_LABEL[item.attention] || item.attention));
        body.append(row);
      }
      table.append(body);
      const wrap = node("div", "tablewrap");
      wrap.append(table);
      card.append(wrap);
    }
    if (report.sidebar_truncated) {
      card.append(
        node(
          "p",
          "triagecard__meta",
          "The sidebar was clipped, so more unread entries may exist below.",
        ),
      );
    }
    holder.append(card);
  }
}

async function startTriage(kvm) {
  const scope = kvm === ALL_KVMS ? "all environments" : kvm;
  const targets =
    kvm === ALL_KVMS ? state.kvms.filter((p) => p.configured).map((p) => p.name) : [kvm];
  try {
    const job = await api("/api/triage", {
      method: "POST",
      body: JSON.stringify({ kvm }),
    }).then((response) => response.json());
    follow(job, `Slack triage · ${scope}`, targets);
  } catch (error) {
    setDrawer("failed", `Slack triage · ${scope}`, error.message);
    openDrawer();
  }
}

/* ---------------- run drawer ---------------- */

function setDrawer(status, title, meta) {
  const drawer = el("drawer");
  drawer.dataset.state = status;
  el("drawer-glyph").textContent =
    status === "running" ? "▸" : status === "succeeded" ? "✓" : status === "failed" ? "✕" : "•";
  el("drawer-title").textContent = title;
  el("drawer-meta").textContent = meta || "";
}

function openDrawer() {
  el("drawer-body").hidden = false;
  el("drawer-toggle").setAttribute("aria-expanded", "true");
}

function renderResults(results) {
  const holder = el("run-results");
  clear(holder);
  for (const result of results) {
    const row = node("div", "resultlist__row");
    row.append(outcomeChip(result.ok ? "success" : "failure"));
    row.append(node("span", "resultlist__kvm", result.kvm));
    row.append(node("span", "resultlist__text", result.text));
    holder.append(row);
  }
}

function appendTrace(text) {
  const trace = el("trace");
  trace.append(document.createTextNode(`${text}\n`));
  trace.scrollTop = trace.scrollHeight;
}

/* EventSource cannot set request headers, and the token must never ride in a URL,
   so the SSE stream is read from fetch() and parsed here. */
async function* sseFrames(path, signal) {
  const response = await api(path, { signal, headers: { Accept: "text/event-stream" } });
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true });
    let split;
    while ((split = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);
      let event = "message";
      const data = [];
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trim());
      }
      if (data.length) yield { event, data: JSON.parse(data.join("\n")) };
    }
  }
}

async function follow(job, label, targets) {
  if (state.stream) state.stream.abort();
  const controller = new AbortController();
  state.stream = controller;
  for (const target of targets) state.busy.add(target);

  clear(el("trace"));
  renderResults([]);
  setDrawer("running", label, "running…");
  openDrawer();
  renderFleet();
  renderAvailability();
  renderTriageButtons();

  try {
    for await (const frame of sseFrames(`/api/jobs/${job.id}/events`, controller.signal)) {
      if (frame.event === "event") {
        appendTrace(frame.data.text);
      } else if (frame.event === "done") {
        renderResults(frame.data.results || []);
        if (frame.data.kind === "triage" && frame.data.payload) {
          state.triage = frame.data.payload;
          renderTriage();
        }
        const ok = frame.data.status === "succeeded";
        setDrawer(
          ok ? "succeeded" : "failed",
          label,
          frame.data.error || frame.data.summary || (ok ? "finished" : "stopped"),
        );
      } else if (frame.event === "timeout") {
        setDrawer("failed", label, "event stream timed out — refresh to see the recorded result");
      }
    }
  } catch (error) {
    if (error.name !== "AbortError") {
      setDrawer("failed", label, `lost the trace stream: ${error.message}`);
    }
  } finally {
    state.stream = null;
    for (const target of targets) state.busy.delete(target);
    await refresh();
  }
}

async function startAvailability(action, kvm) {
  const scope = kvm === ALL_KVMS ? "all environments" : kvm;
  const label =
    action === "get" ? `Check availability · ${scope}` : `Set ${action} · ${scope}`;
  const payload = { kvm };
  if (action !== "get") payload.availability = action;
  const targets =
    kvm === ALL_KVMS ? state.kvms.filter((p) => p.configured).map((p) => p.name) : [kvm];
  try {
    const job = await api("/api/availability", {
      method: "POST",
      body: JSON.stringify(payload),
    }).then((response) => response.json());
    follow(job, label, targets);
  } catch (error) {
    setDrawer("failed", label, error.message);
    openDrawer();
  }
}

async function startSchedule(action, availability) {
  const label = `Schedule ${action}${availability ? ` · ${availability}` : ""}`;
  const payload = { action };
  if (availability) payload.availability = availability;
  const targets =
    action === "run-now" || action === "reconcile"
      ? state.kvms.filter((p) => p.configured).map((p) => p.name)
      : [];
  try {
    const job = await api("/api/schedule/actions", {
      method: "POST",
      body: JSON.stringify(payload),
    }).then((response) => response.json());
    follow(job, label, targets);
  } catch (error) {
    setDrawer("failed", label, error.message);
    openDrawer();
  }
}

/* ---------------- screenshot ---------------- */

async function captureScreenshot() {
  const name = el("shot-kvm").value;
  if (!name) {
    el("shot-meta").textContent = "No configured KVM available.";
    return;
  }
  const holder = el("shot-holder");
  const button = el("shot-capture");
  button.disabled = true;
  el("shot-meta").textContent = `Capturing ${name}…`;
  try {
    const response = await api(`/api/kvms/${encodeURIComponent(name)}/screenshot`);
    const width = response.headers.get("X-Screen-Width");
    const height = response.headers.get("X-Screen-Height");
    const blob = await response.blob();
    clear(holder);
    const image = document.createElement("img");
    image.alt = `Live PiKVM frame from ${name}`;
    image.src = URL.createObjectURL(blob);
    image.addEventListener("load", () => URL.revokeObjectURL(image.src), { once: true });
    holder.append(image);
    el("shot-meta").textContent = `${name} · ${width}×${height} · ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    el("shot-meta").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

/* ---------------- sections ---------------- */

function showSection(name) {
  state.section = name;
  for (const view of document.querySelectorAll(".view")) {
    view.hidden = view.dataset.view !== name;
  }
  for (const item of document.querySelectorAll(".navitem[data-section]")) {
    item.classList.toggle("is-active", item.dataset.section === name);
  }
  try {
    localStorage.setItem("work-agent-section", name);
  } catch {
    /* private browsing */
  }
}

/* ---------------- refresh ---------------- */

function historyPath(kvm) {
  const query = new URLSearchParams({ days: String(state.range), limit: "300" });
  if (kvm) query.set("kvm", kvm);
  return `/api/history?${query}`;
}

async function refresh() {
  const content = el("content");
  content.classList.add("is-refreshing");
  try {
    const [fleetHistory, schedule] = await Promise.all([
      getJSON(historyPath(null)),
      getJSON("/api/schedule"),
    ]);
    state.fleetHistory = fleetHistory;
    state.schedule = schedule;
    state.activityHistory = state.activityKvm
      ? await getJSON(historyPath(state.activityKvm))
      : fleetHistory;

    renderSchedule();
    renderFleet();
    renderOverview();
    renderAvailability();
    renderTriageButtons();
    renderActivity();
  } catch (error) {
    setDrawer("failed", "Could not load dashboard data", error.message);
    openDrawer();
  } finally {
    content.classList.remove("is-refreshing");
  }
}

/* ---------------- theme ---------------- */

const THEMES = ["auto", "light", "dark"];

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  el("theme-label").textContent = theme[0].toUpperCase() + theme.slice(1);
  try {
    localStorage.setItem("work-agent-theme", theme);
  } catch {
    /* private browsing */
  }
}

/* ---------------- boot ---------------- */

async function boot() {
  let storedTheme = "auto";
  let storedSection = "overview";
  try {
    storedTheme = localStorage.getItem("work-agent-theme") || "auto";
    storedSection = localStorage.getItem("work-agent-section") || "overview";
  } catch {
    /* private browsing */
  }
  applyTheme(THEMES.includes(storedTheme) ? storedTheme : "auto");

  el("theme-toggle").addEventListener("click", () => {
    const current = document.documentElement.dataset.theme;
    applyTheme(THEMES[(THEMES.indexOf(current) + 1) % THEMES.length]);
  });

  for (const item of document.querySelectorAll(".navitem[data-section]")) {
    item.addEventListener("click", () => showSection(item.dataset.section));
  }
  for (const button of document.querySelectorAll("[data-goto]")) {
    button.addEventListener("click", () => showSection(button.dataset.goto));
  }
  for (const button of document.querySelectorAll("[data-toggle-table]")) {
    button.addEventListener("click", () => {
      const key = button.dataset.toggleTable;
      const table = el(`${key}-table`);
      const showTable = table.hidden;
      table.hidden = !showTable;
      el(key).hidden = showTable;
      button.textContent = showTable ? "Chart" : "Table";
    });
  }

  el("drawer-toggle").addEventListener("click", () => {
    const body = el("drawer-body");
    body.hidden = !body.hidden;
    el("drawer-toggle").setAttribute("aria-expanded", String(!body.hidden));
  });

  // Availability buttons live in the fleet rail and the per-environment table, so
  // delegate rather than rebinding on every render.
  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-action][data-kvm]");
    if (trigger && !trigger.disabled) {
      startAvailability(trigger.dataset.action, trigger.dataset.kvm);
      return;
    }
    const triage = event.target.closest("[data-triage]");
    if (triage && !triage.disabled) startTriage(triage.dataset.triage);
  });

  for (const button of document.querySelectorAll("[data-schedule]")) {
    button.addEventListener("click", () => {
      const action = button.dataset.schedule;
      if (action === "uninstall" && !window.confirm("Remove the three generated LaunchAgents?")) {
        return;
      }
      startSchedule(action, button.dataset.availability);
    });
  }

  el("range-select").addEventListener("change", (event) => {
    state.range = Number(event.target.value);
    refresh();
  });
  el("refresh").addEventListener("click", refresh);
  el("shot-capture").addEventListener("click", captureScreenshot);

  const config = await getJSON("/api/config");
  state.kvms = config.kvms;
  el("footer-paths").textContent =
    `log ${config.log_path} · state ${config.state_path} · schedule timezone ${config.timezone}`;

  const shotSelect = el("shot-kvm");
  clear(shotSelect);
  for (const profile of state.kvms.filter((item) => item.configured)) {
    const option = node("option", null, profile.name);
    option.value = profile.name;
    shotSelect.append(option);
  }
  if (config.default_kvm) shotSelect.value = config.default_kvm;

  renderTriageButtons();
  renderTriage();
  showSection(storedSection);
  await refresh();
  setInterval(() => {
    if (!state.stream) refresh();
  }, 60000);
}

boot().catch((error) => {
  setDrawer("failed", "Dashboard failed to start", error.message);
  openDrawer();
});
