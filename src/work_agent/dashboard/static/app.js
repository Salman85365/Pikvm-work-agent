const TOKEN = document.querySelector('meta[name="dashboard-token"]').content;
const ALL_KVMS = "__all__";
const SCREEN_RETENTION_MS = 5 * 60 * 1000;

const el = (id) => document.getElementById(id);

const PAGE_META = {
  overview: {
    title: "Overview",
    summary: "Fleet health and recent availability work.",
  },
  availability: {
    title: "Availability",
    summary: "Check or change only Slack's manual Active/Away toggle.",
  },
  triage: {
    title: "Inbox & triage",
    summary: "Observe visible unread signals without opening a conversation.",
  },
  agenda: {
    title: "Today's meetings",
    summary: "Read an already-open calendar without opening or joining a meeting.",
  },
  meetings: {
    title: "Meeting recorder",
    summary: "Record the remote computer's audio over PiKVM, then get a transcript, action items, and a report.",
  },
  schedule: {
    title: "Schedule",
    summary: "Keep availability aligned with the nightly Asia/Karachi window.",
  },
  profiles: {
    title: "Profiles",
    summary: "Name, enable, test, and enroll the PiKVMs this Mac may drive. Secrets stay in Keychain.",
  },
  activity: {
    title: "History",
    summary: "Review sanitized checks, changes, and stops.",
  },
  screen: {
    title: "Remote screen",
    summary: "Capture one temporary, read-only PiKVM frame.",
  },
};

const JOB_KIND_LABELS = {
  triage: "Slack triage",
  agenda: "Today's meetings",
  availability_get: "Check availability",
  availability_set: "Set availability",
  schedule_run_now: "Test scheduled state",
  schedule_reconcile: "Sync scheduled state",
  schedule_install: "Repair scheduler",
  schedule_uninstall: "Remove scheduler",
  meeting_start: "Start recording",
  meeting_stop: "Stop & process meeting",
};

const state = {
  config: null,
  meetings: null,
  meetingsError: null,
  meetingDetail: null,
  meetingDetailId: null,
  meetingBusy: null,
  meetingTicker: null,
  section: "overview",
  range: 7,
  kvms: [],
  fleetHistory: null,
  currentHistory: null,
  activityHistory: null,
  schedule: null,
  activityKvm: null,
  activityOutcome: "all",
  busy: new Set(),
  capabilities: [],
  triage: null,
  triageCapturedAt: null,
  triageHidden: false,
  agenda: null,
  agendaCapturedAt: null,
  agendaHidden: false,
  agendaPending: false,
  agendaSourceJob: null,
  agendaSourceStartedAt: null,
  jobs: new Map(),
  streams: new Map(),
  selectedJob: null,
  notices: new Map(),
  refreshPromise: null,
  refreshQueued: false,
  refreshGeneration: 0,
  lastUpdatedAt: null,
  lastRefreshFailures: 0,
  profiles: null,
  profilesError: null,
  profileBusy: new Set(),
  totp: { name: null, busy: false, file: null },
  confirms: new Map(),
  shot: {
    url: null,
    name: null,
    width: null,
    height: null,
    capturedAt: null,
    timer: null,
    stale: false,
    controller: null,
    generation: 0,
  },
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
  if (!target) return;
  while (target.firstChild) target.removeChild(target.firstChild);
}

function plural(count, noun) {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
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
    ? chip("success", "✓", "Completed")
    : chip("failure", "✕", "Stopped");
}

function jobStatusChip(status) {
  if (status === "running") return chip("muted", "▸", "Running");
  if (status === "succeeded") return chip("success", "✓", "Completed");
  if (status === "partial") return chip("warn", "!", "Completed with issues");
  if (status === "cancelled") return chip("muted", "■", "Cancelled");
  return chip("failure", "✕", "Stopped");
}

function formatTokens(count) {
  if (!Number.isFinite(count)) return "0";
  if (count >= 1000000) return `${(count / 1000000).toFixed(1)}M`;
  if (count >= 1000) return `${(count / 1000).toFixed(count >= 10000 ? 0 : 1)}k`;
  return String(count);
}

function formatRuntime(seconds) {
  if (!Number.isFinite(seconds)) return "0 s";
  if (seconds >= 90) return `${(seconds / 60).toFixed(1)} min`;
  return `${Math.round(seconds)} s`;
}

/* Telemetry is numbers only: how much work a run took, never what it saw. */
function telemetryText(telemetry) {
  if (!telemetry) return "";
  return [
    plural(telemetry.steps || 0, "step"),
    `${telemetry.hid_actions || 0} HID`,
    `${formatTokens(telemetry.total_tokens || 0)} tokens`,
    formatRuntime(telemetry.runtime_seconds || 0),
  ].join(" · ");
}

function relativeTime(iso) {
  if (!iso) return "never";
  const seconds = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (!Number.isFinite(seconds)) return "never";
  if (seconds < 0) return "just now";
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
  const value = new Date(iso);
  if (!Number.isFinite(value.getTime())) return "—";
  return value.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function fullTime(iso) {
  if (!iso) return "";
  const value = new Date(iso);
  if (!Number.isFinite(value.getTime())) return "";
  return value.toLocaleString(undefined, {
    weekday: "short",
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    timeZoneName: "short",
  });
}

/* A timestamp shown one way (relative or local) with the full local time on hover. */
function timeNode(iso, { relative = true, className = "" } = {}) {
  const element = node("time", className, relative ? relativeTime(iso) : localTime(iso));
  if (iso) {
    element.dateTime = iso;
    element.title = fullTime(iso);
  }
  return element;
}

/* ---------------- toasts, busy buttons, inline confirms ---------------- */

const TOAST_MS = 6000;

function toast(kind, message, { sticky = false } = {}) {
  const holder = el("toasts");
  if (!holder) return null;
  const item = node("div", `toast toast--${kind}`);
  item.append(node("span", "toast__glyph", kind === "success" ? "✓" : kind === "danger" ? "✕" : kind === "warn" ? "!" : "•"));
  item.append(node("span", "toast__text", message));
  const close = node("button", "toast__close", "×");
  close.type = "button";
  close.setAttribute("aria-label", "Dismiss notification");
  const dismiss = () => {
    item.classList.add("is-leaving");
    window.setTimeout(() => item.remove(), 160);
  };
  close.addEventListener("click", dismiss);
  item.append(close);
  holder.append(item);
  while (holder.children.length > 4) holder.firstChild.remove();
  if (!sticky) window.setTimeout(dismiss, TOAST_MS);
  return item;
}

/* Buttons show their own busy state and cannot be double-submitted. */
function setButtonBusy(button, busy, busyLabel) {
  if (!button) return;
  if (busy) {
    if (!button.dataset.idleLabel) button.dataset.idleLabel = button.textContent;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.classList.add("is-busy");
    clear(button);
    button.append(node("span", "spinner", ""));
    button.append(node("span", null, busyLabel || button.dataset.idleLabel));
  } else {
    button.disabled = false;
    button.removeAttribute("aria-busy");
    button.classList.remove("is-busy");
    if (button.dataset.idleLabel) {
      button.textContent = button.dataset.idleLabel;
      delete button.dataset.idleLabel;
    }
  }
}

/* Inline confirmation rendered next to the control, never window.confirm(). */
function inlineConfirm(host, { text, confirmLabel, onConfirm, danger = true }) {
  if (!host) return;
  clear(host);
  host.hidden = false;
  host.append(node("span", "confirm__text", text));
  const actions = node("div", "confirm__actions");
  const yes = node("button", `button button--small${danger ? " button--danger" : " button--primary"}`, confirmLabel);
  yes.type = "button";
  const no = node("button", "button button--small button--quiet", "Cancel");
  no.type = "button";
  const close = () => {
    host.hidden = true;
    clear(host);
  };
  yes.addEventListener("click", () => {
    close();
    onConfirm();
  });
  no.addEventListener("click", close);
  actions.append(yes, no);
  host.append(actions);
  yes.focus();
}

function detailText(record) {
  if (record.error) return record.error;
  if (record.changed === true) return "State changed and visually verified";
  if (record.changed === false && record.desired) return "Already correct; no click sent";
  return "Verified from the visible toggle";
}

function isReady(profile) {
  return Boolean(profile?.configured && !profile.interactive_totp);
}

function readyProfiles() {
  return state.kvms.filter(isReady);
}

function profileProblem(profile) {
  if (!profile.configured) return profile.problem || "Configuration is incomplete.";
  if (profile.interactive_totp) {
    return "Interactive TOTP cannot run in the dashboard. Enroll this profile in Keychain first.";
  }
  return profile.problem || null;
}

function announce(message) {
  let region = el("task-announcer") || el("announcer");
  if (!region) {
    region = node("div", "visually-hidden");
    region.id = "announcer";
    region.setAttribute("aria-live", "polite");
    region.setAttribute("aria-atomic", "true");
    document.body.append(region);
  }
  // The task drawer may be collapsed with [hidden]; keep its concise status
  // region outside that inaccessible subtree while leaving the raw trace quiet.
  if (region.closest("[hidden]")) document.body.append(region);
  region.textContent = "";
  window.setTimeout(() => {
    region.textContent = message;
  }, 20);
}

function setNotice(key, message) {
  state.notices.set(key, message);
  renderNotices();
}

function clearNotice(key) {
  state.notices.delete(key);
  renderNotices();
}

function renderNotices() {
  const holder = el("data-notices");
  if (!holder) return;
  clear(holder);
  holder.hidden = state.notices.size === 0;
  for (const message of state.notices.values()) holder.append(node("p", null, message));
}

/* ---------------- tooltip ---------------- */

const tooltip = el("tooltip");

function bindTooltip(target, value, label) {
  if (!tooltip) return;
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

/* ---------------- current-state truth ---------------- */

function outcomeFor(kvm, payload = state.fleetHistory) {
  const summary = payload?.summary;
  return summary?.per_kvm.find((item) => item.kvm === kvm) || null;
}

function currentStateFor(kvm) {
  const latestHistory = outcomeFor(kvm, state.currentHistory);
  const historyCandidate = latestHistory?.last_at
    ? latestHistory.last_outcome === "success"
      ? {
          value: latestHistory.last_observed,
          at: latestHistory.last_at,
          source: "Verified operation",
        }
      : {
          value: null,
          at: latestHistory.last_at,
          source: "Latest check stopped",
        }
    : null;

  const appliedValue = state.schedule?.applied?.[kvm] || null;
  const appliedAt = state.schedule?.applied_verified_at?.[kvm] || null;
  const scheduleCandidate =
    appliedValue && appliedAt
      ? { value: appliedValue, at: appliedAt, source: "Scheduler record" }
      : null;

  if (historyCandidate && scheduleCandidate) {
    const historyTime = new Date(historyCandidate.at).getTime();
    const scheduleTime = new Date(scheduleCandidate.at).getTime();
    return scheduleTime > historyTime ? scheduleCandidate : historyCandidate;
  }
  if (historyCandidate) return historyCandidate;
  if (scheduleCandidate) return scheduleCandidate;
  if (appliedValue) return { value: appliedValue, at: null, source: "Scheduler record" };
  return { value: null, at: null, source: "No verified state" };
}

function currentStateText(kvm) {
  const current = currentStateFor(kvm);
  return current.at ? `${current.source} · ${relativeTime(current.at)}` : current.source;
}

/* ---------------- fleet liveness (from /api/schedule) ---------------- */

function kvmStatusFor(kvm) {
  return (state.schedule?.kvms || []).find((item) => item.name === kvm) || null;
}

function reachabilityChip(status) {
  if (!status || status.reachable === null || status.reachable === undefined) {
    return chip("unknown", null, "Reachability unknown");
  }
  if (status.reachable) return chip("success", "✓", "Reachable");
  const since = status.unreachable_since || status.checked_at;
  return chip("failure", "✕", since ? `Unreachable since ${localTime(since)}` : "Unreachable");
}

function livenessChips(status) {
  const chips = [];
  if (!status) return chips;
  const reach = reachabilityChip(status);
  bindTooltip(
    reach,
    status.reachability_detail
      ? `Unauthenticated probe: ${status.reachability_detail}`
      : "Not probed",
    status.checked_at ? `checked ${relativeTime(status.checked_at)}` : "",
  );
  chips.push(reach);
  if (status.workflow_running) {
    chips.push(chip("warn", "▸", "A scheduled/CLI workflow is running now"));
  }
  if (status.consecutive_failures >= 2) {
    chips.push(chip("failure", "!", `${status.consecutive_failures} failed in a row`));
  }
  return chips;
}

function renderAlerts() {
  const holder = el("fleet-alerts");
  if (!holder) return;
  clear(holder);
  const alerts = (state.schedule?.kvms || []).filter((item) => item.alert);
  holder.hidden = alerts.length === 0;
  for (const status of alerts) {
    const alert = status.alert;
    const box = node("div", "alert");
    box.append(node("span", "alert__glyph", "✕"));
    const body = node("div", "alert__body");
    body.append(
      node(
        "div",
        "alert__title",
        `${status.name}: ${alert.label} — ${plural(alert.count, "run")} in a row stopped`,
      ),
    );
    const meta = [];
    if (alert.since) meta.push(`since ${localTime(alert.since)} (${relativeTime(alert.since)})`);
    if (status.reachable === false) meta.push("still unreachable now");
    else if (status.reachable === true) meta.push("endpoint answers now; the next run may recover");
    if (status.workflow_running) meta.push("a workflow is running now");
    body.append(node("div", "alert__meta", meta.join(" · ") || "no probe yet"));
    if (alert.reason) body.append(node("div", "alert__reason", alert.reason));
    box.append(body);
    holder.append(box);
  }
}

/* ---------------- fleet rail ---------------- */

function profileCardFor(name) {
  return (state.profiles || []).find((item) => item.name === name) || null;
}

/* Credential posture from /api/profiles: enrolled / not enrolled / no 2FA. Never a secret. */
function profileChips(card, { withEnroll = false } = {}) {
  const chips = [];
  if (!card) return chips;
  if (!card.enabled) chips.push(chip("muted", "■", "Disabled"));
  if (card.totp_required && card.totp_enrolled) chips.push(chip("success", "✓", "2FA enrolled"));
  else if (card.totp_required) {
    const warning = chip("warn", "!", "2FA not enrolled");
    chips.push(warning);
    if (withEnroll && card.enabled) {
      const enroll = node("button", "button button--small button--inline", "Enroll 2FA");
      enroll.type = "button";
      enroll.dataset.profileEnroll = card.name;
      enroll.disabled = !state.capabilities.includes("profiles");
      chips.push(enroll);
    }
  } else chips.push(chip("muted", "•", "No 2FA"));
  return chips;
}

function disabledProfiles() {
  return (state.profiles || []).filter((card) => !card.enabled);
}

function availabilityButton(action, profile, { small = true } = {}) {
  const labels = {
    get: "Check",
    active: "Set active",
    away: "Set away",
  };
  const button = node("button", `button${small ? " button--small" : ""}`, labels[action]);
  button.type = "button";
  button.dataset.action = action;
  button.dataset.kvm = profile.name;
  button.setAttribute("aria-label", `${labels[action]} for ${profile.name}`);
  button.disabled = state.busy.has(profile.name) || !isReady(profile);
  if (!isReady(profile)) button.title = profileProblem(profile) || "This environment is not ready.";
  return button;
}

function renderFleet() {
  const holder = el("fleet");
  if (!holder) return;
  clear(holder);
  const required = state.schedule?.desired_now || null;
  const ready = readyProfiles().length;
  const configured = state.kvms.filter((profile) => profile.configured).length;
  const disabled = disabledProfiles();
  holder.removeAttribute("aria-busy");
  el("fleet-hint").textContent = state.kvms.length
    ? `${ready} ready · ${configured} configured · ${state.kvms.length} enabled` +
      (disabled.length ? ` · ${disabled.length} disabled` : "")
    : disabled.length
      ? `No enabled profiles · ${disabled.length} disabled`
      : "No profiles yet";

  if (!state.kvms.length && !disabled.length) {
    const empty = node("div", "empty empty--action");
    empty.append(node("p", null, "No PiKVM profiles yet. Add one to start controlling a work computer."));
    const go = node("button", "button button--primary button--small", "Add a profile");
    go.type = "button";
    go.dataset.goto = "profiles";
    empty.append(go);
    holder.append(empty);
    return;
  }

  for (const profile of state.kvms) {
    const outcome = outcomeFor(profile.name);
    const current = currentStateFor(profile.name);
    const busy = state.busy.has(profile.name);
    const drifted = Boolean(required && current.value && current.value !== required);

    const card = node("article", "kvmcard");
    if (busy) card.classList.add("is-busy");
    else if (drifted) card.classList.add("is-drifted");

    const top = node("div", "kvmcard__top");
    const identity = node("div");
    identity.append(node("div", "kvmcard__name", profile.name));
    identity.append(node("div", "kvmcard__host", profile.endpoint || "endpoint unavailable"));
    top.append(identity);

    const stateBox = node("div", "kvmcard__state");
    stateBox.append(availabilityChip(current.value, { large: true }));
    if (!isReady(profile)) {
      stateBox.append(chip("failure", "✕", profile.configured ? "Needs Keychain" : "Not ready"));
    } else if (busy) {
      stateBox.append(chip("muted", "▸", "Running"));
    } else if (drifted) {
      stateBox.append(chip("warn", "!", `Schedule wants ${required}`));
    } else if (required && current.value) {
      stateBox.append(chip("success", "✓", "Matches schedule"));
    }
    top.append(stateBox);
    card.append(top);

    const liveness = [
      ...livenessChips(kvmStatusFor(profile.name)),
      ...profileChips(profileCardFor(profile.name), { withEnroll: true }),
    ];
    if (liveness.length) {
      const live = node("div", "kvmcard__live");
      for (const item of liveness) live.append(item);
      card.append(live);
    }

    if (outcome) {
      const percent = Math.round(outcome.success_rate * 100);
      const counted = outcome.total - (outcome.skipped || 0);
      const meter = node("div", "kvmcard__meter");
      meter.tabIndex = 0;
      meter.setAttribute("role", "img");
      meter.setAttribute("aria-label", `${profile.name}: ${percent}% of ${counted} runs completed`);
      const head = node("div", "kvmcard__meterhead");
      head.append(node("span", null, "Completed runs"));
      const value = node("span");
      value.append(node("b", null, `${percent}%`));
      value.append(document.createTextNode(` of ${counted}`));
      head.append(value);
      meter.append(head);
      const track = node("div", "meter__track");
      const fill = node("div", "meter__fill");
      fill.style.width = `${percent}%`;
      track.append(fill);
      meter.append(track);
      bindTooltip(
        meter,
        `${percent}% completed`,
        `${outcome.success} completed · ${outcome.failure} stopped` +
          (outcome.skipped ? ` · ${outcome.skipped} skipped (PiKVM busy)` : "") +
          " in the selected history range",
      );
      card.append(meter);
    } else {
      card.append(node("div", "kvmcard__note", "No runs in the selected history range."));
    }

    const actions = node("div", "kvmcard__actions");
    for (const action of ["get", "active", "away"]) {
      actions.append(availabilityButton(action, profile));
    }
    const quick = node("div", "kvmcard__quick");
    if (state.capabilities.includes("triage")) {
      const triage = node("button", "button button--small button--quiet", "Triage");
      triage.type = "button";
      triage.dataset.triage = profile.name;
      triage.setAttribute("aria-label", `Triage Slack inbox on ${profile.name}`);
      triage.disabled = busy || !isReady(profile);
      quick.append(triage);
    }
    if (state.capabilities.includes("screenshot")) {
      const shot = node("button", "button button--small button--quiet", "Screenshot");
      shot.type = "button";
      shot.dataset.quickShot = profile.name;
      shot.setAttribute("aria-label", `Capture one temporary frame from ${profile.name}`);
      shot.disabled = !isReady(profile);
      quick.append(shot);
    }
    if (quick.childElementCount) actions.append(quick);
    card.append(actions);

    const note = node("div", "kvmcard__note kvmcard__note--last");
    const problem = profileProblem(profile);
    if (problem) note.textContent = problem;
    else {
      const current = currentStateFor(profile.name);
      note.append(document.createTextNode(current.source));
      if (current.at) {
        note.append(document.createTextNode(" · "));
        note.append(timeNode(current.at));
      }
    }
    card.append(note);
    holder.append(card);
  }

  for (const item of disabled) {
    const card = node("article", "kvmcard kvmcard--disabled");
    const top = node("div", "kvmcard__top");
    const identity = node("div");
    identity.append(node("div", "kvmcard__name", item.name));
    identity.append(node("div", "kvmcard__host", item.host || item.url));
    top.append(identity);
    const stateBox = node("div", "kvmcard__state");
    stateBox.append(chip("muted", "■", "Disabled", { large: true }));
    top.append(stateBox);
    card.append(top);
    const live = node("div", "kvmcard__live");
    for (const extra of profileChips(item).slice(1)) live.append(extra);
    live.append(chip("muted", "•", item.source === "env" ? "from .env" : "managed"));
    card.append(live);
    const actions = node("div", "kvmcard__actions");
    const enable = node("button", "button button--small", "Enable");
    enable.type = "button";
    enable.dataset.profileAction = "enable";
    enable.dataset.profileName = item.name;
    enable.disabled = state.profileBusy.has(item.name);
    actions.append(enable);
    const manage = node("button", "button button--small button--quiet", "Manage");
    manage.type = "button";
    manage.dataset.goto = "profiles";
    actions.append(manage);
    card.append(actions);
    card.append(node("div", "kvmcard__note kvmcard__note--last", "Excluded from every run target and from the schedule."));
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
    ? `${summary.success} of ${summary.total - (summary.skipped || 0)} runs` +
      (summary.skipped ? ` · ${summary.skipped} skipped (PiKVM busy)` : "")
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
    cell.colSpan = 7;
    row.append(cell);
    body.append(row);
    return;
  }
  for (const record of recent) body.append(historyRow(record));
}

function renderBars(holder, items, labelOf, emptyNoun) {
  if (!holder) return;
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
    bindTooltip(bar, `${plural(item.count, "run")}`, label);
    holder.append(bar);
  }
}

function renderCountTable(holder, items, heading, labelOf) {
  if (!holder) return;
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
  if (!items.length) {
    const row = node("tr");
    const cell = node("td", "table__detail", "No entries in this range.");
    cell.colSpan = 2;
    row.append(cell);
    body.append(row);
  } else {
    for (const item of items) {
      const row = node("tr");
      row.append(node("td", null, item.count));
      row.append(node("td", "table__detail", labelOf(item)));
      body.append(row);
    }
  }
  table.append(body);
  holder.append(table);
}

/* ---------------- availability section ---------------- */

function renderBatchAvailabilityActions() {
  const buttons = document.querySelectorAll(`[data-action][data-kvm="${ALL_KVMS}"]`);
  const ready = readyProfiles().length;
  const anyBusy = state.kvms.some((profile) => state.busy.has(profile.name));
  const labels = {
    get: `Check ${plural(ready, "ready environment")}`,
    active: `Set ${plural(ready, "ready environment")} Active`,
    away: `Set ${plural(ready, "ready environment")} Away`,
  };
  for (const button of buttons) {
    button.textContent = labels[button.dataset.action];
    button.setAttribute("aria-label", labels[button.dataset.action]);
    button.disabled = ready === 0 || anyBusy;
  }
  const first = buttons[0];
  const note = first?.parentElement?.querySelector(".actions__note");
  if (note) {
    const skipped = state.kvms.length - ready;
    note.textContent = skipped
      ? `Runs one environment at a time; ${plural(skipped, "unready environment")} will be reported in the task result.`
      : "Runs one environment at a time; one failure does not stop the rest.";
  }
}

function renderAvailability() {
  const body = el("avail-body");
  if (!body) return;
  clear(body);
  const required = state.schedule?.desired_now || null;

  if (!state.kvms.length) {
    const row = node("tr");
    const cell = node("td", "table__detail", "No environments are listed.");
    cell.colSpan = 5;
    row.append(cell);
    body.append(row);
  }

  for (const profile of state.kvms) {
    const current = currentStateFor(profile.name);
    const row = node("tr");
    row.append(node("td", "table__kvm", profile.name));

    const observedCell = node("td");
    observedCell.append(availabilityChip(current.value));
    row.append(observedCell);

    const wantedCell = node("td");
    wantedCell.append(availabilityChip(required));
    row.append(wantedCell);

    const verifiedCell = node("td", "table__detail");
    verifiedCell.textContent = profileProblem(profile) || currentStateText(profile.name);
    row.append(verifiedCell);

    const actionCell = node("td", "table__actions");
    for (const action of ["get", "active", "away"]) {
      actionCell.append(availabilityButton(action, profile));
    }
    row.append(actionCell);
    body.append(row);
  }
  renderBatchAvailabilityActions();
}

/* ---------------- schedule section ---------------- */

function latestScheduleJob() {
  return [...state.jobs.values()]
    .filter((job) => job.kind === "schedule_reconcile" || job.kind === "schedule_run_now")
    .sort((a, b) => new Date(b.started_at) - new Date(a.started_at))[0] || null;
}

function jobStatusLabel(job) {
  if (!job) return "No attempt in this dashboard session";
  if (job.status === "running") return job.cancel_requested ? "Cancelling" : "Running";
  if (job.status === "partial") return "Completed with issues";
  if (job.status === "succeeded") return "Completed";
  if (job.status === "cancelled") return "Cancelled";
  return "Stopped";
}

/* Green only when the scheduler is healthy AND every environment's verified state matches
   the rule right now; a healthy scheduler with drifted or unverified environments is amber. */
function scheduleStatus(snapshot) {
  const ready = readyProfiles().length;
  const listed = state.kvms.length;
  const environmentProblems = state.kvms
    .filter((profile) => !isReady(profile))
    .map((profile) => `${profile.name}: ${profileProblem(profile)}`);
  const allEnvironmentsReady = listed > 0 && ready === listed;
  const environmentsNeedingVerification = state.kvms.filter(
    (profile) =>
      isReady(profile) && currentStateFor(profile.name).value !== snapshot.desired_now,
  ).length;
  const scheduleVerified =
    allEnvironmentsReady && environmentsNeedingVerification === 0;
  const automationHealthy = snapshot.healthy && scheduleVerified;
  const automationPartial = snapshot.healthy && ready > 0 && !scheduleVerified;
  const kind = automationHealthy ? "good" : automationPartial ? "warning" : "critical";
  const glyph = automationHealthy ? "✓" : automationPartial ? "!" : "✕";
  const label = !snapshot.healthy
    ? `Schedule needs attention (${snapshot.problems.length})`
    : listed === 0
      ? "No environments listed"
      : ready === 0
        ? "No environments ready"
        : !allEnvironmentsReady
          ? `${ready} of ${listed} environments ready`
          : environmentsNeedingVerification
            ? `Schedule needs verification (${environmentsNeedingVerification})`
            : "Schedule verified";
  return {
    kind,
    glyph,
    label,
    chipKind: automationHealthy ? "success" : automationPartial ? "warn" : "failure",
    healthy: automationHealthy,
    partial: automationPartial,
    problems: [...snapshot.problems, ...environmentProblems],
    ready,
    listed,
  };
}

function renderSchedule() {
  const snapshot = state.schedule;
  if (!snapshot) return;

  const status = scheduleStatus(snapshot);
  const { ready, listed } = status;
  const problemsForDisplay = status.problems;
  const automationPartial = status.partial;
  const automationHealthy = status.healthy;
  const statusKind = status.kind;
  const statusGlyph = status.glyph;
  const statusLabel = status.label;

  const navDot = el("nav-schedule-dot");
  if (navDot) navDot.dataset.kind = statusKind;

  const pill = el("schedule-pill");
  pill.className = `pill pill--${statusKind}`;
  clear(pill);
  pill.append(node("span", "pill__icon", statusGlyph));
  pill.append(node("span", "pill__text", statusLabel));

  const automation = el("sched-automation");
  if (automation) {
    automation.className = `chip chip--${automationHealthy ? "success" : automationPartial ? "warn" : "failure"}`;
    clear(automation);
    automation.append(node("span", "chip__glyph", statusGlyph));
    automation.append(node("span", "chip__name", statusLabel));
  }

  const required = el("required-pill");
  required.className = "pill pill--series";
  clear(required);
  required.append(node("span", "pill__icon", "●"));
  required.append(node("span", "pill__text", `Slack should be ${snapshot.desired_now} now`));

  const problems = el("schedule-problems");
  clear(problems);
  problems.hidden = !problemsForDisplay.length;
  if (problemsForDisplay.length) {
    const title = node("div", "problems__title");
    title.append(node("span", null, automationPartial ? "!" : "✕"));
    title.append(node("span", null, "Availability automation needs attention"));
    problems.append(title);
    const list = node("ul");
    for (const problem of problemsForDisplay) list.append(node("li", null, problem));
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
      timeZone: "Asia/Karachi",
    });

  if (el("sched-environments")) {
    el("sched-environments").textContent = `${ready} ready of ${listed} listed`;
  }
  const lastAttempt = latestScheduleJob();
  if (el("sched-last-attempt")) {
    el("sched-last-attempt").textContent = lastAttempt
      ? `${localTime(lastAttempt.started_at)} · ${relativeTime(lastAttempt.started_at)}`
      : "No dashboard sync recorded";
  }
  if (el("sched-last-outcome")) {
    el("sched-last-outcome").textContent = jobStatusLabel(lastAttempt);
  }
  const lastRun = el("sched-last-run");
  if (lastRun) {
    clear(lastRun);
    if (snapshot.last_run) {
      lastRun.append(outcomeChip(snapshot.last_run.outcome));
      lastRun.append(
        node(
          "span",
          "rowlist__meta",
          ` ${snapshot.last_run.kvm} · ${localTime(snapshot.last_run.at)} · ${relativeTime(snapshot.last_run.at)}` +
            (snapshot.last_run.stop_code ? ` · ${snapshot.last_run.stop_code.replace(/_/g, " ")}` : ""),
        ),
      );
    } else {
      lastRun.textContent = "No run recorded in the operation log";
    }
  }
  const fleet = el("sched-fleet");
  if (fleet) {
    clear(fleet);
    const statuses = snapshot.kvms || [];
    if (!statuses.length) fleet.append(node("li", null, "No environment status available."));
    for (const status of statuses) {
      const item = node("li");
      const identity = node("span");
      identity.append(node("span", "rowlist__name", status.name));
      const meta = status.last_run_at
        ? `last run ${relativeTime(status.last_run_at)} · ` +
          (status.last_run_outcome === "success" ? "completed" : "stopped") +
          (status.consecutive_failures ? ` · ${plural(status.consecutive_failures, "failure")} in a row` : "")
        : "no run recorded";
      identity.append(node("span", "rowlist__meta", meta));
      item.append(identity);
      const chips = node("span", "rowlist__chips");
      for (const live of livenessChips(status)) chips.append(live);
      item.append(chips);
      fleet.append(item);
    }
  }

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
  el("sched-interp").textContent = snapshot.interpreter || "No interpreter recorded";

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
    const identity = node("span");
    identity.append(node("span", "rowlist__name", kvm));
    const at = snapshot.applied_verified_at?.[kvm] || null;
    identity.append(node("span", "rowlist__meta", at ? `verified ${relativeTime(at)}` : "time unavailable"));
    item.append(identity);
    item.append(availabilityChip(value));
    applied.append(item);
  }
  el("applied-when").textContent = entries.length ? "per-environment verification times" : "";

  const anyKvmBusy = state.kvms.some((profile) => state.busy.has(profile.name));
  const scheduleBusy = state.busy.has("__schedule__");
  const sync = el("schedule-sync");
  if (sync) {
    sync.textContent = `Sync ${plural(ready, "ready environment")} now`;
    sync.disabled = ready === 0 || anyKvmBusy;
  }
  const repair = el("schedule-repair");
  if (repair) {
    repair.textContent = snapshot.healthy ? "Reinstall scheduler" : "Repair scheduler";
    repair.disabled = scheduleBusy;
  }
  const remove = el("schedule-remove");
  if (remove) remove.disabled = scheduleBusy || !snapshot.installed;
  for (const button of document.querySelectorAll('[data-schedule="run-now"]')) {
    button.disabled = ready === 0 || anyKvmBusy;
  }
}

/* ---------------- activity section ---------------- */

function historyRow(record) {
  const row = node("tr");
  const when = node("td", "table__when");
  when.append(timeNode(record.timestamp, { relative: false }));
  row.append(when);
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
  row.append(node("td", "table__effort", record.telemetry ? telemetryText(record.telemetry) : "—"));
  return row;
}

function filterButton(label, pressed, onClick) {
  const button = node("button", "filterchip", label);
  button.type = "button";
  button.classList.toggle("is-on", pressed);
  button.setAttribute("aria-pressed", String(pressed));
  button.addEventListener("click", onClick);
  return button;
}

function renderActivityFilters() {
  const filters = el("history-filters");
  clear(filters);
  filters.setAttribute("aria-label", "Filter availability history");

  filters.append(node("span", "card__hint", "Environment"));
  const environments = [{ name: null, label: "All" }].concat(
    state.kvms.map((profile) => ({ name: profile.name, label: profile.name })),
  );
  for (const option of environments) {
    filters.append(
      filterButton(option.label, state.activityKvm === option.name, () => {
        state.activityKvm = option.name;
        refresh();
      }),
    );
  }

  filters.append(node("span", "card__hint", "Outcome"));
  for (const option of [
    ["all", "All"],
    ["success", "Completed"],
    ["failure", "Stopped"],
  ]) {
    filters.append(
      filterButton(option[1], state.activityOutcome === option[0], () => {
        state.activityOutcome = option[0];
        renderActivity();
      }),
    );
  }
}

function renderActivity() {
  const payload = state.activityHistory;
  if (!payload) return;

  renderBars(el("reasons"), payload.summary.failure_reasons, (item) => item.reason, "stops recorded");
  renderCountTable(el("reasons-table"), payload.summary.failure_reasons, "Stop reason", (item) => item.reason);
  renderActivityFilters();

  const records = payload.records.filter(
    (record) => state.activityOutcome === "all" || record.outcome === state.activityOutcome,
  );
  const filterSuffix = state.activityOutcome === "all" ? "" : ` · ${state.activityOutcome === "success" ? "completed only" : "stopped only"}`;
  el("history-hint").textContent = records.length
    ? `showing ${records.length} of ${payload.summary.total}${filterSuffix}` +
      (payload.unreadable_lines ? ` · ${payload.unreadable_lines} unreadable line(s)` : "")
    : payload.log_present
      ? `nothing matches these filters${filterSuffix}`
      : "no operation log yet";

  const body = el("history-body");
  clear(body);
  if (!records.length) {
    const row = node("tr");
    const cell = node("td", "table__detail", "No availability runs match these filters.");
    cell.colSpan = 7;
    row.append(cell);
    body.append(row);
    return;
  }
  for (const record of records) body.append(historyRow(record));
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
  for (const profile of readyProfiles()) {
    const button = node("button", "button button--small", `Check ${profile.name}`);
    button.type = "button";
    button.dataset.triage = profile.name;
    button.setAttribute("aria-label", `Check visible Slack unread signals for ${profile.name}`);
    button.disabled = state.busy.has(profile.name) || !state.capabilities.includes("triage");
    holder.append(button);
  }

  const batch = document.querySelector(`[data-triage="${ALL_KVMS}"]`);
  if (batch) {
    const ready = readyProfiles().length;
    batch.textContent = `Check ${plural(ready, "ready environment")}`;
    batch.setAttribute("aria-label", `${batch.textContent} for visible Slack unread signals`);
    batch.disabled =
      ready === 0 ||
      !state.capabilities.includes("triage") ||
      state.kvms.some((profile) => state.busy.has(profile.name));
  }
}

function appendTriageTable(card, heading, items) {
  card.append(node("h3", "subhead", `${heading} · ${items.length}`));
  if (!items.length) {
    card.append(node("p", "triagecard__meta", heading === "Needs attention" ? "None." : "Nothing else unread."));
    return;
  }
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
  for (const item of items) {
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

function renderTriage() {
  const holder = el("triage-body");
  clear(holder);
  const payload = state.triage;
  const captured = el("triage-captured-at");
  const clearButton = el("triage-clear");
  if (captured) {
    captured.textContent = state.triageCapturedAt
      ? `Captured ${localTime(state.triageCapturedAt)} · ${relativeTime(state.triageCapturedAt)}`
      : "Not checked yet";
  }
  if (clearButton) clearButton.disabled = !payload;

  if (!payload) {
    holder.append(node("p", "empty", "No triage run yet. Reading the sidebar never opens a conversation."));
    return;
  }
  for (const report of payload.reports || []) {
    const card = node("section", "triagecard");
    const head = node("div", "triagecard__head");
    head.append(node("span", "triagecard__kvm", report.kvm));
    if (!report.success) {
      head.append(chip("failure", "✕", report.error || "Triage unavailable"));
      card.append(head);
      holder.append(card);
      continue;
    }
    const attention = report.items.filter(
      (item) => item.attention === "mentioned" || item.attention === "direct" || item.has_mention,
    );
    const informational = report.items.filter((item) => !attention.includes(item));
    head.append(chip(attention.length ? "warn" : "success", attention.length ? "!" : "✓", `${attention.length} need attention`));
    head.append(
      node(
        "span",
        "triagecard__meta",
        `${report.items.length} unread · confidence ${Math.round(report.confidence * 100)}%`,
      ),
    );
    card.append(head);

    if (report.sidebar_obstructed || report.sidebar_truncated) {
      const warning = node("div", "problems");
      const title = node("div", "problems__title");
      title.append(node("span", null, "!"));
      title.append(node("span", null, "This result may be incomplete"));
      warning.append(title);
      const list = node("ul");
      if (report.sidebar_obstructed) {
        list.append(node("li", null, "Something covered part of the Slack sidebar."));
      }
      if (report.sidebar_truncated) {
        list.append(node("li", null, "The sidebar was clipped; more unread entries may exist below."));
      }
      warning.append(list);
      card.append(warning);
    }

    if (!report.items.length) {
      card.append(node("p", "triagecard__meta", "Nothing unread."));
    } else {
      appendTriageTable(card, "Needs attention", attention);
      appendTriageTable(card, "Other unread", informational);
    }
    holder.append(card);
  }
}

function clearTriage() {
  state.triage = null;
  state.triageCapturedAt = null;
  state.triageHidden = true;
  renderTriage();
  announce("Triage results hidden from this panel.");
}

/* ---------------- today's meetings ---------------- */

const MEETING_STATUS_LABEL = {
  in_progress: "In progress",
  upcoming: "Upcoming",
  unknown: "Time unclear",
  ended: "Ended",
};

function renderAgendaButtons() {
  const holder = el("agenda-per-kvm");
  clear(holder);
  for (const profile of readyProfiles()) {
    const button = node("button", "button button--small", `Read ${profile.name}`);
    button.type = "button";
    button.dataset.agenda = profile.name;
    button.setAttribute("aria-label", `Read today's visible calendar for ${profile.name}`);
    button.disabled = state.busy.has(profile.name) || !state.capabilities.includes("agenda");
    holder.append(button);
  }

  const batch = document.querySelector(`[data-agenda="${ALL_KVMS}"]`);
  if (batch) {
    const ready = readyProfiles().length;
    batch.textContent = `Read ${plural(ready, "ready environment")}`;
    batch.setAttribute("aria-label", `${batch.textContent} for today's visible calendar`);
    batch.disabled =
      ready === 0 ||
      !state.capabilities.includes("agenda") ||
      state.kvms.some((profile) => state.busy.has(profile.name));
  }
}

function meetingWhen(item) {
  if (item.all_day) return "All day";
  if (!item.start_text) return "—";
  return item.end_text ? `${item.start_text}–${item.end_text}` : item.start_text;
}

function appendAgendaTable(card, heading, items) {
  card.append(node("h3", "subhead", `${heading} · ${items.length}`));
  const table = node("table", "table");
  const thead = node("thead");
  const headRow = node("tr");
  for (const label of ["When", "Meeting", "Where", "Status"]) {
    const cell = node("th", null, label);
    cell.scope = "col";
    headRow.append(cell);
  }
  thead.append(headRow);
  table.append(thead);
  const body = node("tbody");
  for (const item of items) {
    const classes = ["agendacard__row"];
    if (item.declined) classes.push("agendacard__row--declined");
    if (item.status === "ended") classes.push("agendacard__row--ended");
    const row = node("tr", classes.join(" "));
    row.append(node("td", "agendacard__when", meetingWhen(item)));
    const title = node("td");
    title.append(node("span", "agendacard__title", item.title));
    if (item.is_online) title.append(node("span", "agendacard__tag", "online"));
    if (item.declined) title.append(node("span", "agendacard__tag", "declined"));
    if (item.organizer) title.append(node("p", "agendacard__meta", item.organizer));
    row.append(title);
    row.append(node("td", "table__detail", item.location || "—"));
    row.append(node("td", null, MEETING_STATUS_LABEL[item.status] || item.status));
    body.append(row);
  }
  table.append(body);
  const wrap = node("div", "tablewrap");
  wrap.append(table);
  card.append(wrap);
}

function renderAgenda() {
  const holder = el("agenda-body");
  clear(holder);
  const payload = state.agenda;
  const captured = el("agenda-captured-at");
  const clearButton = el("agenda-clear");
  if (captured) {
    captured.textContent = state.agendaPending
      ? "Reading now…"
      : state.agendaCapturedAt
        ? `Checked ${localTime(state.agendaCapturedAt)} · ${relativeTime(state.agendaCapturedAt)}`
        : "Not checked yet";
  }
  if (clearButton) clearButton.disabled = !payload;

  if (!payload) {
    holder.append(
      node(
        "p",
        "empty",
        state.agendaPending
          ? "Reading the visible calendar…"
          : "No calendar read yet. Open your calendar on the remote machine first — this never opens one, and never joins a meeting.",
      ),
    );
    return;
  }
  for (const report of payload.reports || []) {
    const card = node("section", "agendacard");
    const head = node("div", "agendacard__head");
    head.append(node("span", "agendacard__kvm", report.kvm));
    if (!report.success) {
      head.append(chip("failure", "✕", report.error || "Calendar unavailable"));
      card.append(head);
      holder.append(card);
      continue;
    }
    const ahead = report.items.filter((item) => item.status !== "ended");
    const earlier = report.items.filter((item) => item.status === "ended");
    const live = ahead.filter((item) => item.status === "in_progress").length;
    head.append(chip("success", "✓", `${ahead.length} still ahead`));
    if (live) head.append(chip("warn", "▸", plural(live, "meeting") + " in progress"));
    const meta = [report.date_text || "today"];
    if (report.current_time_text) meta.push(`clock ${report.current_time_text}`);
    meta.push(`confidence ${Math.round(report.confidence * 100)}%`);
    head.append(node("span", "agendacard__meta", meta.join(" · ")));
    card.append(head);

    const cautions = [];
    if (report.obstructed) cautions.push("Something covered part of the calendar.");
    if (report.later_truncated) {
      cautions.push("The day was still clipped; later meetings may exist below.");
    }
    if (report.earlier_truncated) {
      cautions.push("The day was clipped above; earlier meetings may exist.");
    }
    if (!report.clock_read) {
      cautions.push(
        "The clock on the remote machine could not be read, so nothing is marked as already over.",
      );
    }
    if (cautions.length) {
      const warning = node("div", "problems");
      const title = node("div", "problems__title");
      title.append(node("span", null, "!"));
      title.append(node("span", null, "This result may be incomplete"));
      warning.append(title);
      const list = node("ul");
      for (const caution of cautions) list.append(node("li", null, caution));
      warning.append(list);
      card.append(warning);
    }

    if (!report.items.length) {
      card.append(node("p", "agendacard__meta", "Nothing on today's visible calendar."));
    } else {
      if (ahead.length) appendAgendaTable(card, "Still ahead", ahead);
      else card.append(node("p", "agendacard__meta", "Nothing further scheduled today."));
      if (earlier.length) appendAgendaTable(card, "Earlier today", earlier);
    }
    holder.append(card);
  }
}

function clearAgenda() {
  state.agenda = null;
  state.agendaCapturedAt = null;
  state.agendaHidden = true;
  state.agendaPending = false;
  renderAgenda();
  announce("Meeting results hidden from this panel.");
}

/* ---------------- recoverable task center ---------------- */

function jobTitle(job) {
  if (job.displayLabel) return job.displayLabel;
  const base = JOB_KIND_LABELS[job.kind] || "Dashboard task";
  const target = job.target === "all KVMs" ? "all environments" : job.target;
  return target ? `${base} · ${target}` : base;
}

function mergeJob(incoming, extras = {}) {
  const previous = state.jobs.get(incoming.id) || {};
  const merged = {
    ...previous,
    ...incoming,
    ...extras,
    events: Array.isArray(incoming.events) ? incoming.events : previous.events || [],
    results: Array.isArray(incoming.results) ? incoming.results : previous.results || [],
    targets: Array.isArray(incoming.targets) ? incoming.targets : previous.targets || [],
  };
  state.jobs.set(merged.id, merged);
  return merged;
}

function syncBusy() {
  const busy = new Set();
  for (const job of state.jobs.values()) {
    if (job.status !== "running") continue;
    for (const target of job.targets || []) busy.add(target);
  }
  state.busy = busy;
}

function sortedJobs() {
  return [...state.jobs.values()].sort((a, b) => new Date(b.started_at) - new Date(a.started_at));
}

function failedJobFingerprint(job) {
  if (job.status !== "failed") return null;
  return JSON.stringify([
    job.kind,
    job.target,
    job.error || null,
    job.summary || null,
    (job.results || []).map((result) => [result.kvm, result.ok, result.text]),
  ]);
}

function compactJobs(jobs) {
  const compacted = [];
  for (const job of jobs) {
    const fingerprint = failedJobFingerprint(job);
    const previous = compacted.at(-1);
    if (fingerprint && previous?.fingerprint === fingerprint) {
      previous.repeatCount += 1;
      previous.groupedIds.push(job.id);
      continue;
    }
    compacted.push({
      ...job,
      fingerprint,
      repeatCount: 1,
      groupedIds: [job.id],
    });
  }
  return compacted;
}

function selectJob(jobId) {
  if (!state.jobs.has(jobId)) return;
  state.selectedJob = jobId;
  renderTaskCenter();
}

function taskRow(job) {
  const row = node("button", "resultlist__row resultlist__task");
  row.type = "button";
  const repetitionLabel =
    job.repeatCount > 1 ? `, ${job.repeatCount} identical stopped attempts` : "";
  row.setAttribute(
    "aria-label",
    `Show ${jobTitle(job)}, ${jobStatusLabel(job)}${repetitionLabel}`,
  );
  row.setAttribute("aria-pressed", String(state.selectedJob === job.id));
  row.append(jobStatusChip(job.status));
  row.append(node("span", "resultlist__kvm", jobTitle(job)));
  const repeated =
    job.repeatCount > 1 ? `${job.repeatCount} identical stopped attempts · ` : "";
  const detail =
    repeated +
    (job.error ||
      job.summary ||
      `${relativeTime(job.started_at)} · ${plural((job.targets || []).length, "target")}`);
  row.append(node("span", "resultlist__text", detail));
  row.addEventListener("click", () => selectJob(job.id));
  return row;
}

function renderTaskCenter() {
  const jobs = compactJobs(sortedJobs());
  if (state.selectedJob && !state.jobs.has(state.selectedJob)) state.selectedJob = null;
  const selectedGroup = state.selectedJob
    ? jobs.find((job) => job.groupedIds.includes(state.selectedJob))
    : null;
  if (selectedGroup) state.selectedJob = selectedGroup.id;
  if (!state.selectedJob && jobs.length) state.selectedJob = jobs[0].id;
  const selected = state.selectedJob ? state.jobs.get(state.selectedJob) : null;
  const running = jobs.filter((job) => job.status === "running");

  const drawer = el("drawer");
  const glyph = el("drawer-glyph");
  if (!jobs.length) {
    drawer.dataset.state = "idle";
    glyph.textContent = "•";
    el("drawer-title").textContent = "Task center";
    el("drawer-meta").textContent = "No runs yet";
  } else if (running.length) {
    drawer.dataset.state = "running";
    glyph.textContent = "▸";
    el("drawer-title").textContent = `Task center · ${plural(running.length, "task")} running`;
    el("drawer-meta").textContent = selected ? jobTitle(selected) : "Open for details";
  } else {
    const status = selected?.status || "succeeded";
    drawer.dataset.state = ["failed", "partial", "cancelled"].includes(status) ? status : "succeeded";
    glyph.textContent =
      status === "failed" ? "✕" : status === "partial" ? "!" : status === "cancelled" ? "■" : "✓";
    el("drawer-title").textContent = "Task center";
    el("drawer-meta").textContent = selected
      ? `${jobStatusLabel(selected)} · ${jobTitle(selected)}`
      : `${plural(jobs.length, "recorded task")}`;
  }

  const controls = el("task-controls");
  const cancelButton = el("job-cancel");
  if (controls && cancelButton) {
    const canCancel =
      Boolean(selected) &&
      selected.status === "running" &&
      Boolean(selected.cancellable) &&
      state.capabilities.includes("job_cancel");
    controls.hidden = !canCancel;
    cancelButton.disabled = !canCancel || Boolean(selected?.cancel_requested);
    cancelButton.textContent = selected?.cancel_requested ? "Cancel requested…" : "Cancel selected task";
    el("job-cancel-note").textContent = canCancel
      ? "Stops before the next retry; a workflow already driving the screen finishes its current step first."
      : "";
  }

  const results = el("run-results");
  clear(results);
  for (const job of jobs) results.append(taskRow(job));
  if (selected?.results?.length) {
    results.append(node("h3", "subhead", `Selected task results · ${selected.results.length}`));
    for (const result of selected.results) {
      const row = node("div", "resultlist__row");
      row.append(jobStatusChip(result.ok ? "succeeded" : "failed"));
      row.append(node("span", "resultlist__kvm", result.kvm));
      row.append(node("span", "resultlist__text", result.text));
      results.append(row);
    }
  }

  const trace = el("trace");
  const followTraceTail = trace.scrollHeight - trace.scrollTop - trace.clientHeight < 32;
  trace.removeAttribute("aria-live");
  trace.setAttribute("role", "log");
  trace.setAttribute("aria-label", selected ? `Technical trace for ${jobTitle(selected)}` : "Technical trace");
  trace.textContent = selected?.events?.length ? `${selected.events.join("\n")}\n` : "";
  if (followTraceTail) trace.scrollTop = trace.scrollHeight;
  updateDrawerHeight();
}

function openDrawer() {
  el("drawer-body").hidden = false;
  el("drawer-toggle").setAttribute("aria-expanded", "true");
  updateDrawerHeight();
}

function updateDrawerHeight() {
  window.requestAnimationFrame(() => {
    const drawer = el("drawer");
    if (drawer) document.documentElement.style.setProperty("--drawer-height", `${Math.ceil(drawer.getBoundingClientRect().height)}px`);
  });
}

function absorbJobPayload(job) {
  if ((job.kind === "meeting_start" || job.kind === "meeting_stop") && job.status !== "running") {
    state.meetingBusy = null;
    loadMeetings({ quiet: true }).then(() => {
      if (job.kind === "meeting_stop" && job.status === "succeeded" && job.payload?.session_id) {
        openMeetingSession(job.payload.session_id);
      }
    });
  }
  if (job.kind === "triage" && job.payload && !state.triageHidden) {
    state.triage = job.payload;
    state.triageCapturedAt = job.finished_at || new Date().toISOString();
    renderTriage();
  }
  if (
    job.kind === "agenda" &&
    job.status !== "running" &&
    !state.agendaHidden &&
    agendaJobIsCurrent(job)
  ) {
    state.agenda = job.payload || failedAgendaPayload(job);
    state.agendaCapturedAt = job.finished_at || new Date().toISOString();
    state.agendaPending = false;
    state.agendaSourceJob = job.id;
    state.agendaSourceStartedAt = job.started_at;
    renderAgenda();
  }
}

function agendaJobIsCurrent(job) {
  if (!state.agendaSourceJob || state.agendaSourceJob === job.id) return true;
  const incoming = new Date(job.started_at).getTime();
  const current = new Date(state.agendaSourceStartedAt).getTime();
  return Number.isFinite(incoming) && (!Number.isFinite(current) || incoming >= current);
}

function markAgendaPending(job) {
  if (!agendaJobIsCurrent(job)) return;
  state.agenda = null;
  state.agendaCapturedAt = null;
  state.agendaPending = true;
  state.agendaSourceJob = job.id;
  state.agendaSourceStartedAt = job.started_at;
}

function failedAgendaPayload(job) {
  const fallback = job.error || job.summary || "Calendar read stopped.";
  const results = job.results?.length
    ? job.results
    : (job.targets || []).map((kvm) => ({ kvm, ok: false, text: fallback }));
  return {
    reports: results.map((result) => ({
      kvm: result.kvm,
      success: false,
      error: result.text || fallback,
    })),
  };
}

/* EventSource cannot set request headers, and the token must never ride in a URL,
   so the SSE stream is read from fetch() and parsed here. */
async function* sseFrames(path, signal) {
  const response = await api(path, { signal, headers: { Accept: "text/event-stream" } });
  if (!response.body) throw new Error("The task stream did not include a response body.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
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

async function refreshJobSnapshot(jobId) {
  const snapshot = await getJSON(`/api/jobs/${jobId}`);
  const job = mergeJob(snapshot);
  clearNotice(`job-${jobId}`);
  absorbJobPayload(job);
  syncBusy();
  renderAll();
  return job;
}

function reconcileJobList(jobs, { restorePanels = false } = {}) {
  clearNotice("jobs-api");
  const incoming = new Set(jobs.map((job) => job.id));
  for (const job of jobs) {
    mergeJob(job);
    clearNotice(`job-${job.id}`);
  }
  for (const [jobId, retained] of state.jobs) {
    if (!incoming.has(jobId) && retained.status !== "running") state.jobs.delete(jobId);
  }
  if (restorePanels) {
    if (!state.triageHidden) {
      const newestTriage = jobs.find((job) => job.kind === "triage" && job.payload);
      if (newestTriage) absorbJobPayload(newestTriage);
    }
    if (!state.agendaHidden) {
      const newestAgenda = jobs.find((job) => job.kind === "agenda");
      if (newestAgenda?.status === "running") markAgendaPending(newestAgenda);
      else if (newestAgenda) absorbJobPayload(newestAgenda);
    }
  }
  syncBusy();
}

function reconnectJob(jobId, delay = 1200) {
  window.setTimeout(() => {
    const job = state.jobs.get(jobId);
    if (job?.status === "running" && !state.streams.has(jobId)) followJob(job);
  }, delay);
}

async function followJob(job, { select = false, open = false } = {}) {
  const merged = mergeJob(job);
  if (select || !state.selectedJob) state.selectedJob = merged.id;
  syncBusy();
  renderAll();
  if (open) openDrawer();
  if (merged.status !== "running") {
    // A job can finish before its own start response is snapshotted - a preflight rejection
    // returns immediately - and then no "done" frame ever arrives to carry its payload.
    absorbJobPayload(merged);
    return;
  }
  if (state.streams.has(merged.id)) return;

  const controller = new AbortController();
  state.streams.set(merged.id, controller);
  const eventCursor = merged.events?.length || 0;
  let completed = false;
  try {
    for await (const frame of sseFrames(
      `/api/jobs/${merged.id}/events?after=${eventCursor}`,
      controller.signal,
    )) {
      if (frame.event === "event") {
        const current = state.jobs.get(merged.id);
        current.events = [...(current.events || []), frame.data.text];
        renderTaskCenter();
      } else if (frame.event === "done") {
        const finished = mergeJob(frame.data);
        absorbJobPayload(finished);
        completed = true;
        const message = `${jobTitle(finished)} ${jobStatusLabel(finished).toLowerCase()}.`;
        announce(message);
        toast(
          finished.status === "succeeded" ? "success" : finished.status === "failed" ? "danger" : "warn",
          message,
        );
      } else if (frame.event === "timeout") {
        const current = await refreshJobSnapshot(merged.id);
        if (current.status === "running") reconnectJob(merged.id);
        else completed = true;
      } else if (frame.event === "gone") {
        // The server pruned this job; drop it here too rather than retrying a dead stream.
        state.jobs.delete(merged.id);
        completed = true;
        setNotice(`job-${merged.id}`, `${jobTitle(merged)} is no longer available on the server.`);
        break;
      }
    }
  } catch (error) {
    if (error.name !== "AbortError") {
      try {
        const current = await refreshJobSnapshot(merged.id);
        if (current.status === "running") reconnectJob(merged.id);
        else completed = true;
      } catch {
        setNotice(`job-${merged.id}`, `Lost the live trace for ${jobTitle(merged)}. Refresh to recover its recorded result.`);
      }
    }
  } finally {
    state.streams.delete(merged.id);
    syncBusy();
    renderAll();
    if (completed) await refresh();
  }
}

async function loadJobs() {
  try {
    const jobs = await getJSON("/api/jobs?limit=50");
    reconcileJobList(jobs, { restorePanels: true });
    if (jobs.length) state.selectedJob = jobs[0].id;
    renderAll();
    for (const job of jobs) {
      if (job.status === "running") followJob(job);
    }
    clearNotice("jobs-api");
  } catch (error) {
    setNotice(
      "jobs-api",
      `Task recovery is unavailable: ${error.message} Restart this local dashboard if it was already running during an update.`,
    );
  }
}

function actionFailed(label, error) {
  setNotice("action", `${label} could not start: ${error.message}`);
  toast("danger", `${label} could not start: ${error.message}`);
  announce(`${label} could not start.`);
}

async function startAvailability(action, kvm) {
  const scope = kvm === ALL_KVMS ? `${plural(readyProfiles().length, "ready environment")}` : kvm;
  const label = action === "get" ? `Check availability · ${scope}` : `Set ${action} · ${scope}`;
  const payload = { kvm };
  if (action !== "get") payload.availability = action;
  try {
    clearNotice("action");
    const job = await api("/api/availability", {
      method: "POST",
      body: JSON.stringify(payload),
    }).then((response) => response.json());
    announce(`${label} started.`);
    followJob(job, { select: true, open: true });
  } catch (error) {
    actionFailed(label, error);
  }
}

async function startTriage(kvm) {
  const scope = kvm === ALL_KVMS ? `${plural(readyProfiles().length, "ready environment")}` : kvm;
  const label = `Slack triage · ${scope}`;
  try {
    clearNotice("action");
    const job = await api("/api/triage", {
      method: "POST",
      body: JSON.stringify({ kvm }),
    }).then((response) => response.json());
    state.triageHidden = false;
    announce(`${label} started.`);
    followJob(job, { select: true, open: true });
  } catch (error) {
    actionFailed(label, error);
  }
}

async function startAgenda(kvm) {
  const scope = kvm === ALL_KVMS ? `${plural(readyProfiles().length, "ready environment")}` : kvm;
  const label = `Today's meetings · ${scope}`;
  try {
    clearNotice("action");
    const job = await api("/api/agenda", {
      method: "POST",
      body: JSON.stringify({ kvm }),
    }).then((response) => response.json());
    state.agendaHidden = false;
    markAgendaPending(job);
    announce(`${label} started.`);
    followJob(job, { select: true, open: true });
  } catch (error) {
    actionFailed(label, error);
  }
}

async function startSchedule(action, availability) {
  const labels = {
    reconcile: "Sync scheduled availability",
    "run-now": `Test scheduled ${availability || "availability"}`,
    install: "Repair scheduler",
    uninstall: "Remove scheduler",
  };
  const label = labels[action] || "Schedule action";
  const payload = { action };
  if (availability) payload.availability = availability;
  try {
    clearNotice("action");
    const job = await api("/api/schedule/actions", {
      method: "POST",
      body: JSON.stringify(payload),
    }).then((response) => response.json());
    announce(`${label} started.`);
    followJob(job, { select: true, open: true });
  } catch (error) {
    actionFailed(label, error);
  }
}

async function cancelJob(jobId) {
  const job = state.jobs.get(jobId);
  if (!job) return;
  try {
    clearNotice("action");
    const snapshot = await api(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, {
      method: "POST",
    }).then((response) => response.json());
    mergeJob(snapshot);
    announce(`Cancel requested for ${jobTitle(job)}.`);
    renderTaskCenter();
  } catch (error) {
    actionFailed(`Cancel ${jobTitle(job)}`, error);
  }
}

/* ---------------- screenshot ---------------- */

function screenshotDescription() {
  if (!state.shot.url) return "No frame captured";
  return `${state.shot.name} · ${state.shot.width}×${state.shot.height} · captured ${localTime(state.shot.capturedAt)}`;
}

function clearScreenshot({ message = "Frame cleared from this browser tab.", announceChange = false } = {}) {
  const generation = state.shot.generation + 1;
  if (state.shot.controller) state.shot.controller.abort();
  if (state.shot.timer) window.clearTimeout(state.shot.timer);
  if (state.shot.url) URL.revokeObjectURL(state.shot.url);
  state.shot = {
    url: null,
    name: null,
    width: null,
    height: null,
    capturedAt: null,
    timer: null,
    stale: false,
    controller: null,
    generation,
  };
  const holder = el("shot-holder");
  clear(holder);
  holder.removeAttribute("data-stale");
  holder.append(node("span", "shot__empty", "No frame captured"));
  el("shot-meta").textContent = message;
  el("shot-capture").disabled =
    !el("shot-kvm").value || !state.capabilities.includes("screenshot");
  el("shot-clear").disabled = true;
  el("shot-expand").disabled = true;
  const dialog = el("shot-dialog");
  if (dialog?.open) dialog.close();
  const dialogImage = el("shot-dialog-image");
  dialogImage.removeAttribute("src");
  dialogImage.hidden = true;
  if (announceChange) announce("Remote frame cleared.");
}

function retainScreenshot(url, name, width, height, generation) {
  if (state.shot.url) URL.revokeObjectURL(state.shot.url);
  if (state.shot.timer) window.clearTimeout(state.shot.timer);
  state.shot = {
    url,
    name,
    width,
    height,
    capturedAt: new Date().toISOString(),
    timer: null,
    stale: false,
    controller: null,
    generation,
  };
  state.shot.timer = window.setTimeout(() => {
    clearScreenshot({ message: "Frame cleared automatically after 5 minutes.", announceChange: true });
  }, SCREEN_RETENTION_MS);
}

function renderScreenshot() {
  const holder = el("shot-holder");
  clear(holder);
  if (!state.shot.url) {
    holder.append(node("span", "shot__empty", "No frame captured"));
    return;
  }
  const image = document.createElement("img");
  image.alt = `Temporary PiKVM frame from ${state.shot.name}`;
  image.src = state.shot.url;
  holder.append(image);
  holder.toggleAttribute("data-stale", state.shot.stale);
  el("shot-clear").disabled = false;
  el("shot-expand").disabled = false;
  el("shot-meta").textContent = state.shot.stale
    ? `Stale frame · ${screenshotDescription()} · the latest capture failed`
    : screenshotDescription();
}

async function captureScreenshot() {
  const name = el("shot-kvm").value;
  if (!name) {
    el("shot-meta").textContent = "No ready KVM is available.";
    return;
  }
  const button = el("shot-capture");
  if (state.shot.controller) state.shot.controller.abort();
  const controller = new AbortController();
  const generation = state.shot.generation + 1;
  state.shot.controller = controller;
  state.shot.generation = generation;
  button.disabled = true;
  el("shot-meta").textContent = `Capturing ${name}…`;
  try {
    const response = await api(`/api/kvms/${encodeURIComponent(name)}/screenshot`, {
      signal: controller.signal,
    });
    const width = response.headers.get("X-Screen-Width") || "?";
    const height = response.headers.get("X-Screen-Height") || "?";
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    if (generation !== state.shot.generation || state.section !== "screen") {
      URL.revokeObjectURL(url);
      return;
    }
    retainScreenshot(url, name, width, height, generation);
    renderScreenshot();
    announce(`Remote frame captured from ${name}.`);
  } catch (error) {
    if (error.name === "AbortError") return;
    if (generation !== state.shot.generation) return;
    if (state.shot.url) {
      state.shot.stale = true;
      renderScreenshot();
    } else {
      el("shot-meta").textContent = `Capture stopped: ${error.message}`;
    }
    announce(`Remote frame capture from ${name} stopped.`);
  } finally {
    if (generation === state.shot.generation) {
      state.shot.controller = null;
      button.disabled = !el("shot-kvm").value;
    }
  }
}

/* Overview quick action: jump to Remote screen with this KVM selected and capture at once. */
function quickCapture(name) {
  const select = el("shot-kvm");
  if (!select || ![...select.options].some((option) => option.value === name)) {
    toast("warn", `${name} is not ready for a screenshot.`);
    return;
  }
  select.value = name;
  showSection("screen", { focus: false });
  captureScreenshot();
}

/* Esc closes whatever transient layer is open: inline confirms, the 2FA panel, then the drawer. */
function closeTransient() {
  const openConfirm = document.querySelector(".confirm:not([hidden])");
  if (openConfirm) {
    openConfirm.hidden = true;
    clear(openConfirm);
    return true;
  }
  const totpPanel = el("totp-panel");
  if (totpPanel && !totpPanel.hidden && !state.totp.busy) {
    closeTotpPanel();
    return true;
  }
  const drawerBody = el("drawer-body");
  if (drawerBody && !drawerBody.hidden) {
    drawerBody.hidden = true;
    el("drawer-toggle").setAttribute("aria-expanded", "false");
    updateDrawerHeight();
    return true;
  }
  return false;
}

function expandScreenshot() {
  if (!state.shot.url) return;
  const dialog = el("shot-dialog");
  const image = el("shot-dialog-image");
  image.src = state.shot.url;
  image.alt = `Full-size temporary PiKVM frame from ${state.shot.name}`;
  image.hidden = false;
  el("shot-dialog-meta").textContent = screenshotDescription();
  dialog.showModal();
}

/* ---------------- overview glance ---------------- */

function renderGlance() {
  const snapshot = state.schedule;
  const health = el("glance-health");
  if (!health) return;
  clear(health);
  if (!snapshot) {
    health.append(chip("unknown", null, "Checking…"));
    return;
  }
  const status = scheduleStatus(snapshot);
  const healthChip = chip(status.chipKind, status.glyph, status.label);
  health.append(healthChip);

  const desired = el("glance-desired");
  clear(desired);
  desired.append(availabilityChip(snapshot.desired_now));

  el("glance-next").textContent =
    `${snapshot.next_transition_to === "active" ? "Active" : "Away"} · ` +
    new Date(snapshot.next_transition_at).toLocaleString(undefined, {
      weekday: "short",
      hour: "2-digit",
      minute: "2-digit",
      timeZone: "Asia/Karachi",
    });

  const lastRun = el("glance-last-run");
  clear(lastRun);
  if (snapshot.last_run) {
    lastRun.append(outcomeChip(snapshot.last_run.outcome));
    lastRun.append(document.createTextNode(` ${snapshot.last_run.kvm} · `));
    lastRun.append(timeNode(snapshot.last_run.at));
  } else {
    lastRun.textContent = "No run recorded yet";
  }

  const profiles = el("glance-profiles");
  clear(profiles);
  if (state.profiles === null) {
    profiles.textContent = state.profilesError ? "unavailable" : "loading…";
  } else {
    const enabled = state.profiles.filter((card) => card.enabled).length;
    const unenrolled = state.profiles.filter(
      (card) => card.enabled && card.totp_required && !card.totp_enrolled,
    ).length;
    profiles.append(
      document.createTextNode(`${enabled} enabled of ${state.profiles.length}`),
    );
    if (unenrolled) {
      profiles.append(document.createTextNode(" · "));
      profiles.append(chip("warn", "!", `${unenrolled} need 2FA enrolment`));
    }
  }
}

/* ---------------- profiles ---------------- */

/* Mirrors the server rule: lowercase slug, 1–40 characters. */
const PROFILE_NAME_PATTERN = /^[a-z0-9][a-z0-9_-]{0,39}$/;

async function loadProfiles() {
  if (!state.capabilities.includes("profiles")) {
    state.profiles = [];
    state.profilesError = "The running server predates the Profiles panel.";
    renderProfiles();
    renderGlance();
    return;
  }
  try {
    const payload = await getJSON("/api/profiles");
    state.profiles = payload.profiles || [];
    state.profilesError = null;
    clearNotice("profiles-api");
  } catch (error) {
    state.profilesError = error.message;
    if (state.profiles === null) state.profiles = [];
    setNotice("profiles-api", `Profiles could not load: ${error.message}`);
  }
  renderProfiles();
  renderFleet();
  renderGlance();
}

/* Every profile change re-reads /api/config as well, so enabled/disabled state flows into the
   run targets, the history filters, and the schedule panel without a page reload. */
async function afterProfileChange() {
  try {
    await loadConfig();
  } catch (error) {
    setNotice("config", `Run targets could not refresh: ${error.message}`);
  }
  await loadProfiles();
  renderAll();
  refresh();
}

function profileBusyKey(name, action) {
  return `${name}:${action}`;
}

function profileRow(card) {
  const row = node("article", "profilecard");
  row.dataset.profile = card.name;
  if (!card.enabled) row.classList.add("is-disabled");
  row.setAttribute("aria-label", `Profile ${card.name}`);

  const head = node("div", "profilecard__head");
  const identity = node("div", "profilecard__identity");
  identity.append(node("h3", "profilecard__name", card.name));
  const host = node("div", "profilecard__host");
  host.append(node("span", "mono", card.url || card.host));
  host.append(document.createTextNode(" · "));
  host.append(node("span", "mono", card.username));
  identity.append(host);
  head.append(identity);

  const chips = node("div", "profilecard__chips");
  chips.append(
    card.enabled ? chip("success", "✓", "Enabled") : chip("muted", "■", "Disabled"),
  );
  for (const item of profileChips(card)) {
    if (item.querySelector(".chip__name")?.textContent === "Disabled") continue;
    chips.append(item);
  }
  chips.append(chip("muted", "•", card.source === "env" ? "from .env" : "managed"));
  chips.append(chip("muted", "•", card.verify_ssl ? "TLS verified" : "TLS unverified"));
  if (card.enabled) {
    const live = kvmStatusFor(card.name);
    if (live) chips.append(reachabilityChip(live));
  }
  head.append(chips);
  row.append(head);

  const actions = node("div", "profilecard__actions");
  const disabledNow = !state.capabilities.includes("profiles");
  const make = (action, label, extraClass = "") => {
    const button = node("button", `button button--small${extraClass}`, label);
    button.type = "button";
    button.dataset.profileAction = action;
    button.dataset.profileName = card.name;
    button.setAttribute("aria-label", `${label} ${card.name}`);
    button.disabled = disabledNow || state.profileBusy.has(profileBusyKey(card.name, action));
    if (state.profileBusy.has(profileBusyKey(card.name, action))) {
      setButtonBusy(button, true, action === "test" ? "Testing…" : `${label}…`);
    }
    return button;
  };
  actions.append(make(card.enabled ? "disable" : "enable", card.enabled ? "Disable" : "Enable"));
  actions.append(make("test", "Test connection"));
  if (card.totp_required && card.enabled) {
    const enroll = node("button", "button button--small", card.totp_enrolled ? "Re-enroll 2FA" : "Enroll 2FA");
    enroll.type = "button";
    enroll.dataset.profileEnroll = card.name;
    enroll.disabled = disabledNow;
    actions.append(enroll);
  }
  if (card.removable) actions.append(make("remove", "Remove", " button--quiet"));
  else {
    actions.append(node("span", "actions__note", "defined in .env — disable instead"));
  }
  row.append(actions);

  const feedback = node("div", "profilecard__feedback");
  feedback.dataset.profileFeedback = card.name;
  feedback.hidden = true;
  row.append(feedback);
  const confirm = node("div", "confirm");
  confirm.dataset.profileConfirm = card.name;
  confirm.hidden = true;
  row.append(confirm);
  return row;
}

function renderProfiles() {
  const holder = el("profiles-list");
  if (!holder) return;
  const hint = el("profiles-hint");
  const count = el("nav-profiles-count");
  if (state.profiles === null) {
    if (hint) hint.textContent = state.profilesError || "Loading profiles…";
    return;
  }
  holder.removeAttribute("aria-busy");
  // Keep open inline feedback and confirmations across re-renders (a test result must not
  // vanish because another profile changed): move the live nodes into the fresh cards.
  const preserved = new Map();
  for (const live of holder.querySelectorAll("[data-profile-feedback]:not([hidden]), [data-profile-confirm]:not([hidden])")) {
    const name = live.dataset.profileFeedback || live.dataset.profileConfirm;
    const kind = live.dataset.profileFeedback ? "feedback" : "confirm";
    preserved.set(`${name}:${kind}`, live);
  }
  clear(holder);
  const enabled = state.profiles.filter((card) => card.enabled).length;
  if (hint) {
    hint.textContent = state.profiles.length
      ? `${enabled} enabled of ${state.profiles.length}`
      : state.profilesError || "No profiles yet";
  }
  if (count) count.textContent = state.profiles.length ? String(state.profiles.length) : "";
  if (!state.profiles.length) {
    holder.append(
      node(
        "p",
        "empty",
        state.profilesError
          ? state.profilesError
          : "No PiKVM profiles yet. Add one with the form, or list names in PIKVM_PROFILES.",
      ),
    );
    return;
  }
  const sorted = [...state.profiles].sort((a, b) => {
    if (a.enabled !== b.enabled) return a.enabled ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
  for (const card of sorted) {
    const row = profileRow(card);
    for (const kind of ["feedback", "confirm"]) {
      const live = preserved.get(`${card.name}:${kind}`);
      if (live) row.querySelector(`[data-profile-${kind}]`).replaceWith(live);
    }
    holder.append(row);
  }
}

function profileFeedback(name, kind, message, { notes = [] } = {}) {
  const target = document.querySelector(`[data-profile-feedback="${CSS.escape(name)}"]`);
  if (!target) return;
  clear(target);
  target.hidden = !message;
  if (!message) return;
  target.dataset.kind = kind;
  target.append(node("div", "profilecard__feedbacktext", message));
  if (notes.length) {
    const list = node("ul", "notes");
    for (const note of notes) list.append(node("li", null, note));
    target.append(list);
  }
}

async function profileAction(name, action) {
  if (action === "remove") {
    const host = document.querySelector(`[data-profile-confirm="${CSS.escape(name)}"]`);
    inlineConfirm(host, {
      text: `Remove profile ${name} and its Keychain entries? This cannot be undone.`,
      confirmLabel: "Remove profile",
      onConfirm: () => removeProfile(name),
    });
    return;
  }
  const key = profileBusyKey(name, action);
  if (state.profileBusy.has(key)) return;
  state.profileBusy.add(key);
  renderProfiles();
  renderFleet();
  const labels = { enable: "Enable", disable: "Disable", test: "Test connection" };
  try {
    const result = await api(`/api/profiles/${encodeURIComponent(name)}/${action}`, {
      method: "POST",
    }).then((response) => response.json());
    const ok = result.ok !== false;
    const extra =
      action === "test" && result.screen_width
        ? ` (${result.screen_width}×${result.screen_height || "?"})`
        : "";
    state.profileBusy.delete(key);
    if (action === "test") {
      toast(ok ? "success" : "danger", `${name}: ${result.message}${extra}`);
      renderProfiles();
      profileFeedback(name, ok ? "success" : "danger", `${result.message}${extra}`);
      announce(`Connection test for ${name} ${ok ? "passed" : "failed"}.`);
    } else {
      toast("success", result.message);
      announce(result.message);
      await afterProfileChange();
    }
  } catch (error) {
    state.profileBusy.delete(key);
    renderProfiles();
    profileFeedback(name, "danger", `${labels[action] || action} failed: ${error.message}`);
    toast("danger", `${name}: ${error.message}`);
    announce(`${labels[action] || action} for ${name} failed.`);
  }
}

async function removeProfile(name) {
  const key = profileBusyKey(name, "remove");
  state.profileBusy.add(key);
  renderProfiles();
  try {
    const result = await api(`/api/profiles/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }).then((response) => response.json());
    state.profileBusy.delete(key);
    if (state.totp.name === name) closeTotpPanel();
    toast("success", [result.message, ...(result.notes || [])].join(" "));
    announce(result.message);
    await afterProfileChange();
  } catch (error) {
    state.profileBusy.delete(key);
    renderProfiles();
    profileFeedback(name, "danger", `Remove failed: ${error.message}`);
    toast("danger", `${name}: ${error.message}`);
  }
}

function showFormError(message) {
  const target = el("profile-form-error");
  if (!target) return;
  target.textContent = message || "";
  target.hidden = !message;
}

async function submitProfileForm(event) {
  event.preventDefault();
  const form = el("profile-form");
  const button = el("profile-add");
  if (!form || button.disabled) return;
  const name = el("profile-name").value.trim();
  const url = el("profile-url").value.trim();
  const username = el("profile-username").value.trim();
  const password = el("profile-password").value;
  const totpRequired = el("profile-totp").checked;
  const verifySsl = el("profile-verify-ssl").checked;

  if (!PROFILE_NAME_PATTERN.test(name)) {
    showFormError("Profile name must be a lowercase slug: a–z, 0–9, dash or underscore, starting with a letter or digit, up to 40 characters.");
    el("profile-name").focus();
    return;
  }
  if (!/^https?:\/\/\S+$/i.test(url)) {
    showFormError("Enter the PiKVM URL including http:// or https://.");
    el("profile-url").focus();
    return;
  }
  if (!username) {
    showFormError("Enter the PiKVM username.");
    el("profile-username").focus();
    return;
  }
  if (!password) {
    showFormError("Enter the PiKVM password. It is stored in Keychain and never shown again.");
    el("profile-password").focus();
    return;
  }
  showFormError("");
  setButtonBusy(button, true, "Adding…");
  try {
    const result = await api("/api/profiles", {
      method: "POST",
      body: JSON.stringify({
        name,
        url,
        username,
        password,
        totp_required: totpRequired,
        verify_ssl: verifySsl,
      }),
    }).then((response) => response.json());
    // The password field is cleared the moment the server has it; nothing echoes it back.
    form.reset();
    el("profile-password").value = "";
    el("profile-totp").checked = true;
    toast("success", result.message);
    announce(result.message);
    await afterProfileChange();
    const created = result.profile;
    if (created) {
      profileFeedback(
        created.name,
        "success",
        created.totp_required && !created.totp_enrolled
          ? "Added. Test the connection, then enroll its 2FA QR."
          : "Added. Test the connection to confirm the credentials.",
      );
      const row = document.querySelector(`[data-profile="${CSS.escape(created.name)}"]`);
      row?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  } catch (error) {
    showFormError(error.message);
    toast("danger", `Add profile failed: ${error.message}`);
  } finally {
    setButtonBusy(button, false);
  }
}

/* ---------------- 2FA (TOTP) enrolment ---------------- */

function openTotpPanel(name) {
  const panel = el("totp-panel");
  if (!panel) return;
  state.totp = { name, busy: false, file: null };
  el("totp-target").textContent = name;
  el("totp-status").textContent = "";
  el("totp-notes").hidden = true;
  clear(el("totp-notes"));
  el("totp-replace").hidden = true;
  el("totp-file").value = "";
  panel.hidden = false;
  showSection("profiles", { focus: false });
  panel.scrollIntoView({ block: "start", behavior: "smooth" });
  el("totp-dropzone").focus({ preventScroll: true });
}

function closeTotpPanel() {
  const panel = el("totp-panel");
  if (!panel) return;
  panel.hidden = true;
  el("totp-file").value = "";
  state.totp = { name: null, busy: false, file: null };
}

function totpStatus(message, kind = "") {
  const target = el("totp-status");
  if (!target) return;
  target.textContent = message;
  target.dataset.kind = kind;
}

function acceptableQrFile(file) {
  if (!file) return "Choose a PNG or JPEG screenshot of the provisioning QR.";
  if (!["image/png", "image/jpeg"].includes(file.type)) return "Only PNG or JPEG images are accepted.";
  if (file.size > 8 * 1024 * 1024) return "The image exceeds 8 MB.";
  return null;
}

async function enrollTotp(file, { replace = false } = {}) {
  const name = state.totp.name;
  if (!name || state.totp.busy) return;
  const problem = acceptableQrFile(file);
  if (problem) {
    totpStatus(problem, "danger");
    return;
  }
  state.totp.busy = true;
  state.totp.file = file;
  el("totp-dropzone").classList.add("is-busy");
  el("totp-dropzone").setAttribute("aria-busy", "true");
  el("totp-replace").hidden = true;
  totpStatus(`Decoding the QR locally and verifying ${name}… this can take up to 20 seconds.`, "busy");
  try {
    // Raw bytes in the body, token in the header; the image is discarded once sent.
    const bytes = await file.arrayBuffer();
    const result = await api(
      `/api/profiles/${encodeURIComponent(name)}/totp?replace=${replace ? "true" : "false"}`,
      { method: "POST", body: bytes, headers: { "Content-Type": file.type } },
    ).then((response) => response.json());
    state.totp.file = null;
    el("totp-file").value = "";
    totpStatus(result.message, "success");
    const notes = el("totp-notes");
    clear(notes);
    for (const note of result.notes || []) notes.append(node("li", null, note));
    notes.hidden = !(result.notes || []).length;
    toast("success", `${name}: ${result.message}`);
    announce(`2FA enrolled for ${name}.`);
    await afterProfileChange();
  } catch (error) {
    if (!replace && /already exists/i.test(error.message)) {
      totpStatus(error.message, "warn");
      el("totp-replace").hidden = false;
      el("totp-replace-confirm").focus();
    } else {
      state.totp.file = null;
      el("totp-file").value = "";
      totpStatus(`Enrolment failed: ${error.message}`, "danger");
      toast("danger", `${name}: ${error.message}`);
    }
  } finally {
    state.totp.busy = false;
    el("totp-dropzone").classList.remove("is-busy");
    el("totp-dropzone").removeAttribute("aria-busy");
  }
}

function bindProfilePanel() {
  el("profile-form")?.addEventListener("submit", submitProfileForm);
  el("profile-name")?.addEventListener("input", (event) => {
    const value = event.target.value;
    event.target.setCustomValidity(
      value && !PROFILE_NAME_PATTERN.test(value) ? "Lowercase letters, digits, dash or underscore only." : "",
    );
  });
  el("profiles-reload")?.addEventListener("click", async () => {
    const button = el("profiles-reload");
    setButtonBusy(button, true, "Reloading…");
    try {
      await afterProfileChange();
    } finally {
      setButtonBusy(button, false);
    }
  });

  el("totp-close")?.addEventListener("click", closeTotpPanel);
  el("totp-file")?.addEventListener("change", (event) => {
    const [file] = event.target.files || [];
    if (file) enrollTotp(file);
  });
  el("totp-replace-confirm")?.addEventListener("click", () => {
    if (state.totp.file) enrollTotp(state.totp.file, { replace: true });
  });
  el("totp-replace-cancel")?.addEventListener("click", () => {
    el("totp-replace").hidden = true;
    state.totp.file = null;
    el("totp-file").value = "";
    totpStatus("Kept the existing seed. Drop another QR to try again.");
  });
  const zone = el("totp-dropzone");
  if (zone) {
    zone.addEventListener("click", (event) => {
      if (event.target.closest("label")) return;
      el("totp-file").click();
    });
    zone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        el("totp-file").click();
      }
    });
    for (const type of ["dragenter", "dragover"]) {
      zone.addEventListener(type, (event) => {
        event.preventDefault();
        zone.classList.add("is-over");
      });
    }
    for (const type of ["dragleave", "drop"]) {
      zone.addEventListener(type, (event) => {
        event.preventDefault();
        zone.classList.remove("is-over");
      });
    }
    zone.addEventListener("drop", (event) => {
      const [file] = event.dataTransfer?.files || [];
      if (file) enrollTotp(file);
    });
  }
  // The page itself must never open a dropped image.
  window.addEventListener("dragover", (event) => event.preventDefault());
  window.addEventListener("drop", (event) => event.preventDefault());
}

/* ---------------- sections ---------------- */

function sectionFromHash() {
  const candidate = window.location.hash.replace(/^#/, "");
  return Object.hasOwn(PAGE_META, candidate) ? candidate : null;
}

function showSection(name, { push = true, focus = true } = {}) {
  if (!Object.hasOwn(PAGE_META, name)) name = "overview";
  if (
    state.section === "screen" &&
    name !== "screen" &&
    (state.shot.url || state.shot.controller)
  ) {
    clearScreenshot({ message: "Frame cleared when you left Remote screen." });
  }
  state.section = name;
  for (const view of document.querySelectorAll(".view")) {
    view.hidden = view.dataset.view !== name;
  }
  for (const item of document.querySelectorAll(".navitem[data-section]")) {
    const active = item.dataset.section === name;
    item.classList.toggle("is-active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  }
  const select = el("section-select");
  if (select) select.value = name;

  const meta = PAGE_META[name];
  const title = el("view-title");
  title.textContent = meta.title;
  el("view-summary").textContent = meta.summary;
  document.title = `${meta.title} · PiKVM Work Agent`;
  document.querySelector(".fleet")?.classList.toggle("fleet--compact", name !== "overview");
  const rangeField = el("range-select")?.closest(".toolbar-field");
  if (rangeField) rangeField.hidden = !["overview", "activity"].includes(name);

  if (push && sectionFromHash() !== name) {
    window.history.pushState({ section: name }, "", `#${name}`);
  }
  try {
    localStorage.setItem("work-agent-section", name);
  } catch {
    /* private browsing */
  }
  // Each section is its own page: start it at the top rather than wherever the previous
  // section had been scrolled to, which left a tall section looking empty.
  if (push) window.scrollTo({ top: 0, behavior: "auto" });
  if (focus) title.focus({ preventScroll: true });
}


/* ---------------- meetings ---------------- */

const RECORDER_PHASE_LABELS = {
  starting: "Starting",
  recording: "Recording",
  stop_requested: "Stopping",
  finalizing: "Finalizing audio",
  ready_for_processing: "Audio saved · ready to process",
  transcribing: "Transcribing",
  analyzing: "Extracting action items",
  processing_failed: "Processing failed",
  completed: "Completed",
  audio_unavailable: "No audio was received",
  disconnected: "Disconnected",
  interrupted: "Interrupted",
  failed: "Failed",
};

function meetingsEnabled() {
  return state.capabilities.includes("meetings");
}

async function loadMeetings({ quiet = false } = {}) {
  if (!meetingsEnabled()) {
    state.meetings = null;
    state.meetingsError = "The running server predates the Meeting recorder panel.";
    renderMeetings();
    return;
  }
  try {
    state.meetings = await getJSON("/api/meetings");
    state.meetingsError = null;
    clearNotice("meetings-api");
  } catch (error) {
    state.meetingsError = error.message;
    if (!quiet) setNotice("meetings-api", `Meeting recorder could not load: ${error.message}`);
  }
  renderMeetings();
  syncMeetingTicker();
}

/* While a recording is live the elapsed counter and phase are re-read every few seconds; the
   ticker stops itself once the recorder is idle so an idle page makes no requests. */
function syncMeetingTicker() {
  const active = Boolean(state.meetings?.recorder?.active);
  if (active && !state.meetingTicker) {
    state.meetingTicker = window.setInterval(() => loadMeetings({ quiet: true }), 5000);
  } else if (!active && state.meetingTicker) {
    window.clearInterval(state.meetingTicker);
    state.meetingTicker = null;
  }
}

function formatClock(seconds) {
  const whole = Math.max(0, Math.round(seconds || 0));
  const h = Math.floor(whole / 3600);
  const m = Math.floor((whole % 3600) / 60);
  const sec = whole % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(sec).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function renderMeetings() {
  const holder = el("recorder-status");
  if (!holder) return;
  const snapshot = state.meetings;
  const recorder = snapshot?.recorder;
  const hint = el("recorder-hint");
  const phase = recorder?.phase || null;
  holder.dataset.phase = phase || "idle";
  let phaseText = "Loading…";
  if (recorder) {
    phaseText = phase
      ? `${RECORDER_PHASE_LABELS[phase] || phase}${recorder.kvm ? ` · ${recorder.kvm}` : ""}`
      : "Idle";
  } else if (state.meetingsError) {
    phaseText = "Unavailable";
  }
  el("recorder-phase").textContent = phaseText;
  el("recorder-elapsed").textContent =
    recorder?.active && recorder.elapsed_seconds ? formatClock(recorder.elapsed_seconds) : "";
  const next = el("recorder-next");
  next.textContent = recorder?.next_step || (recorder && !recorder.active ? "No recording is active." : "");
  if (recorder?.worker_stale) {
    next.textContent = "The recorder process is no longer running; Stop recovers the finalized audio.";
  }
  if (recorder?.error_code) next.textContent += ` (error code: ${recorder.error_code})`;
  if (hint) {
    hint.textContent = snapshot
      ? `transcription: ${snapshot.transcription_provider}${snapshot.transcription_configured ? "" : " (not configured)"}`
      : state.meetingsError || "";
  }
  el("meetings-dir").textContent = snapshot ? `Artifacts: ${snapshot.data_directory}` : "";

  // KVM picker keeps its selection across refreshes.
  const picker = el("meeting-kvm");
  const previous = picker.value;
  picker.replaceChildren();
  for (const profile of readyProfiles()) {
    const option = document.createElement("option");
    option.value = profile.name;
    option.textContent = profile.name;
    picker.append(option);
  }
  if ([...picker.options].some((option) => option.value === previous)) picker.value = previous;
  else if (state.config?.default_kvm && [...picker.options].some((o) => o.value === state.config.default_kvm)) {
    picker.value = state.config.default_kvm;
  }

  const enabled = meetingsEnabled() && Boolean(snapshot);
  const active = Boolean(recorder?.active);
  const stale = Boolean(recorder?.worker_stale);
  const canStart = enabled && !active && picker.options.length > 0 && !state.meetingBusy;
  const canStop = enabled && (active || stale) && !state.meetingBusy;
  const startButton = el("meeting-start");
  const stopButton = el("meeting-stop");
  // setButtonBusy(false) re-enables a button, so apply the busy state first and the
  // availability rule afterwards.
  setButtonBusy(startButton, state.meetingBusy === "start", "Starting…");
  setButtonBusy(stopButton, state.meetingBusy === "stop", "Processing…");
  startButton.disabled = !canStart;
  stopButton.disabled = !canStop;
  picker.disabled = !enabled || active || Boolean(state.meetingBusy);
  const dot = el("nav-meetings-dot");
  if (dot) {
    if (active) dot.dataset.kind = "warning";
    else delete dot.dataset.kind;
  }

  // Setup problems are stated plainly, with the remedy.
  const setup = el("meeting-setup");
  const problems = [];
  if (snapshot && !snapshot.transcription_configured) {
    problems.push(
      snapshot.transcription_provider === "deepgram"
        ? "DEEPGRAM_API_KEY is empty in .env — recording works, but Stop cannot transcribe until it is set."
        : "OPENAI_API_KEY is empty in .env — recording works, but Stop cannot transcribe until it is set.",
    );
  }
  if (snapshot) {
    const missingIdentity = Object.entries(snapshot.identity_configured || {})
      .filter(([, configured]) => !configured)
      .map(([name]) => name);
    if (missingIdentity.length) {
      problems.push(
        `No work identity for ${missingIdentity.join(", ")} (PIKVM_<NAME>_WORK_IDENTITY_NAME in .env): action items cannot be attributed to you and will all land under “other”.`,
      );
    }
  }
  if (state.meetingsError) problems.push(state.meetingsError);
  setup.hidden = problems.length === 0;
  setup.replaceChildren(...problems.map((text) => node("p", "", text)));

  renderSessionList();
}

function renderSessionList() {
  const holder = el("sessions-list");
  if (!holder) return;
  holder.setAttribute("aria-busy", state.meetings ? "false" : "true");
  const sessions = state.meetings?.sessions || [];
  const hint = el("sessions-hint");
  if (hint) hint.textContent = sessions.length ? plural(sessions.length, "recording") : "";
  holder.replaceChildren();
  if (!state.meetings) return;
  if (!sessions.length) {
    holder.append(
      node(
        "div",
        "sessionlist__empty",
        "No recordings yet. Pick an environment and press Start while the meeting audio is playing on that computer.",
      ),
    );
    return;
  }
  const stageLabels = {
    complete: ["success", "✓", "Report ready"],
    report_missing: ["warn", "!", "Report missing"],
    analysis_pending: ["warn", "!", "Needs analysis"],
    transcription_pending: ["warn", "!", "Needs processing"],
  };
  for (const session of sessions) {
    const row = node("button", "sessionrow");
    row.type = "button";
    row.dataset.sessionId = session.session_id;
    if (session.session_id === state.meetingDetailId) row.classList.add("is-selected");
    const main = node("div", "sessionrow__main");
    const title = node("div", "sessionrow__title");
    const when = new Date(session.started_at);
    title.append(node("span", "", when.toLocaleString([], { dateStyle: "medium", timeStyle: "short" })));
    title.append(node("span", "mono", session.kvm));
    main.append(title);
    const meta = node("div", "sessionrow__meta");
    const bits = [formatClock(session.duration_seconds), plural(session.parts, "part")];
    if (session.interrupted) bits.push("interrupted");
    if (session.problem) bits.push(session.problem);
    meta.textContent = bits.join(" · ");
    meta.title = session.session_id;
    main.append(meta);
    row.append(main);
    const chips = node("div", "sessionrow__chips");
    const [kind, glyph, label] = stageLabels[session.stage] || ["muted", "•", session.stage];
    chips.append(chip(kind, glyph, label));
    if (session.our_action_items != null) {
      chips.append(chip(session.our_action_items ? "success" : "muted", "•", `${session.our_action_items} for you`));
      if (session.possible_our_action_items) {
        chips.append(chip("warn", "•", `${session.possible_our_action_items} possibly yours`));
      }
      chips.append(chip("muted", "•", plural(session.decisions || 0, "decision")));
    }
    row.append(chips);
    holder.append(row);
  }
}

async function openMeetingSession(sessionId) {
  const card = el("meeting-detail");
  const body = el("meeting-detail-body");
  state.meetingDetailId = sessionId;
  renderSessionList();
  card.hidden = false;
  card.setAttribute("aria-busy", "true");
  body.replaceChildren(node("p", "muted", "Loading report…"));
  el("meeting-detail-hint").textContent = sessionId;
  try {
    state.meetingDetail = await getJSON(`/api/meetings/sessions/${encodeURIComponent(sessionId)}`);
  } catch (error) {
    state.meetingDetail = null;
    body.replaceChildren(node("p", "", `The report could not be loaded: ${error.message}`));
    card.setAttribute("aria-busy", "false");
    return;
  }
  card.setAttribute("aria-busy", "false");
  renderMeetingDetail();
  card.scrollIntoView({ behavior: "smooth", block: "start" });
}

function closeMeetingDetail() {
  state.meetingDetail = null;
  state.meetingDetailId = null;
  el("meeting-detail").hidden = true;
  renderSessionList();
}

/* The report is scanned, not read: every category has its own color, glyph, and card shape, the
   headline numbers sit in one stat strip, and each item leads with its text - metadata (owner,
   due, timestamp) hangs below as pills. Timestamp pills jump into the transcript. */

const REPORT_SECTIONS = {
  ours: { label: "Your action items", glyph: "◉", kind: "ours", hint: "assigned to you by name" },
  possible: { label: "Possibly yours", glyph: "◎", kind: "possible", hint: "indirect wording — judge yourself" },
  others: { label: "Other people's action items", glyph: "○", kind: "others", hint: "" },
  decisions: { label: "Decisions", glyph: "✓", kind: "decision", hint: "" },
  blockers: { label: "Blockers & risks", glyph: "⚠", kind: "blocker", hint: "" },
  questions: { label: "Open questions", glyph: "?", kind: "question", hint: "" },
  followups: { label: "Follow-ups", glyph: "↻", kind: "followup", hint: "" },
};

function reportSection(key, count) {
  const spec = REPORT_SECTIONS[key];
  const section = node("section", "rp__section");
  section.dataset.kind = spec.kind;
  const head = node("header", "rp__head");
  const badge = node("span", "rp__badge", spec.glyph);
  badge.setAttribute("aria-hidden", "true");
  head.append(badge);
  head.append(node("h3", "rp__title", spec.label));
  head.append(node("span", "rp__count", String(count)));
  if (spec.hint) head.append(node("span", "rp__hinttext", spec.hint));
  section.append(head);
  const list = node("ul", "rp__list");
  section.append(list);
  return { section, list };
}

function reportPill(className, glyph, text, title) {
  const pill = node("span", `rp__pill ${className}`.trim());
  if (glyph) pill.append(node("span", "rp__pillglyph", glyph));
  pill.append(node("span", "", text));
  if (title) pill.title = title;
  return pill;
}

function actionItemCard(item, kind) {
  const li = node("li", "rp__item");
  li.dataset.kind = kind;
  li.append(node("p", "rp__task", item.task));
  const meta = node("div", "rp__meta");
  if (item.owner) meta.append(reportPill("rp__pill--owner", "", item.owner, "Owner"));
  else if (kind === "others") meta.append(reportPill("rp__pill--owner is-unknown", "", "owner unclear"));
  if (item.requested_by) meta.append(reportPill("", "←", `asked by ${item.requested_by}`));
  if (item.due_text) meta.append(reportPill("rp__pill--due", "⏰", item.due_text));
  if (item.timestamp_seconds != null) {
    const jump = node("button", "rp__pill rp__pill--time");
    jump.type = "button";
    jump.dataset.transcriptJump = String(item.timestamp_seconds);
    jump.append(node("span", "rp__pillglyph", "▸"));
    jump.append(node("span", "mono", formatClock(item.timestamp_seconds)));
    jump.title = "Show this moment in the transcript";
    meta.append(jump);
  }
  if (meta.childElementCount) li.append(meta);
  if (item.reason) li.append(node("p", "rp__reason", item.reason));
  return li;
}

function renderMeetingDetail() {
  const detail = state.meetingDetail;
  const body = el("meeting-detail-body");
  if (!detail || !body) return;
  const session = detail.session;
  el("meeting-detail-hint").textContent =
    `${session.kvm} · ${new Date(session.started_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}`;
  body.replaceChildren();

  const items = detail.action_items || [];
  const ours = items.filter((item) => item.owner_category === "our_identity");
  const possible = items.filter((item) => item.owner_category === "possibly_our_identity");
  const others = items.filter((item) => !ours.includes(item) && !possible.includes(item));

  // Headline strip: what happened, in numbers, before any prose.
  const stats = node("div", "rp__stats");
  const stat = (value, label, kind) => {
    const box = node("div", "rp__stat");
    if (kind) box.dataset.kind = kind;
    box.append(node("span", "rp__statvalue", String(value)));
    box.append(node("span", "rp__statlabel", label));
    return box;
  };
  stats.append(stat(formatClock(session.duration_seconds), "duration"));
  const speakers = new Set((detail.transcript || []).map((line) => line.speaker));
  if (speakers.size) stats.append(stat(speakers.size, plural(speakers.size, "speaker").replace(/^\d+ /, "")));
  stats.append(stat(ours.length, "for you", ours.length ? "ours" : ""));
  if (possible.length) stats.append(stat(possible.length, "possibly yours", "possible"));
  stats.append(stat(others.length, "for others", ""));
  stats.append(stat((detail.decisions || []).length, "decisions", (detail.decisions || []).length ? "decision" : ""));
  if ((detail.blockers || []).length) stats.append(stat(detail.blockers.length, "blockers", "blocker"));
  if ((detail.open_questions || []).length) stats.append(stat(detail.open_questions.length, "open questions", "question"));
  body.append(stats);

  if (detail.meeting_summary) {
    const lede = node("section", "rp__lede");
    lede.append(node("h3", "rp__ledetitle", "Summary"));
    lede.append(node("p", "rp__ledetext", detail.meeting_summary));
    body.append(lede);
  } else {
    body.append(
      node(
        "p",
        "muted",
        session.stage === "complete"
          ? "This session has a report but its structured analysis could not be read."
          : "This recording has not been processed yet. Press Stop & process to transcribe it and extract action items.",
      ),
    );
  }

  const columns = node("div", "rp__columns");
  const actionsColumn = node("div", "rp__column");
  const contextColumn = node("div", "rp__column");
  for (const [key, groupItems] of [
    ["ours", ours],
    ["possible", possible],
    ["others", others],
  ]) {
    if (!groupItems.length) continue;
    const { section, list } = reportSection(key, groupItems.length);
    for (const item of groupItems) list.append(actionItemCard(item, REPORT_SECTIONS[key].kind));
    actionsColumn.append(section);
  }
  if (!items.length && detail.meeting_summary) {
    actionsColumn.append(node("p", "rp__empty", "No action items were assigned in this meeting."));
  }
  for (const [key, texts] of [
    ["decisions", detail.decisions],
    ["blockers", detail.blockers],
    ["questions", detail.open_questions],
    ["followups", detail.follow_ups],
  ]) {
    if (!texts?.length) continue;
    const { section, list } = reportSection(key, texts.length);
    for (const text of texts) {
      const li = node("li", "rp__item rp__item--plain");
      li.dataset.kind = REPORT_SECTIONS[key].kind;
      li.append(node("p", "rp__task", text));
      list.append(li);
    }
    contextColumn.append(section);
  }
  if (actionsColumn.childElementCount) columns.append(actionsColumn);
  if (contextColumn.childElementCount) columns.append(contextColumn);
  if (columns.childElementCount) body.append(columns);

  if (detail.transcript?.length) {
    const fold = document.createElement("details");
    fold.className = "rp__fold";
    fold.id = "meeting-transcript-fold";
    fold.append(node("summary", "", `Transcript · ${plural(detail.transcript.length, "line")}`));
    const box = node("div", "rp__transcript");
    const speakerOrder = [...speakers];
    for (const line of detail.transcript) {
      const row = node("div", "rp__line");
      row.dataset.start = String(line.start_seconds);
      row.append(node("span", "rp__linetime mono", formatClock(line.start_seconds)));
      const who = node("span", "rp__speaker", line.speaker);
      who.dataset.speaker = String(speakerOrder.indexOf(line.speaker) % 6);
      row.append(who);
      row.append(node("span", "rp__linetext", line.text));
      box.append(row);
    }
    fold.append(box);
    body.append(fold);
  }

  if (detail.report_markdown) {
    const fold = document.createElement("details");
    fold.className = "rp__fold";
    fold.append(node("summary", "", "report.md · as written to disk"));
    fold.append(node("pre", "rp__markdown", detail.report_markdown));
    body.append(fold);
  }
  el("meeting-copy-report").disabled = !detail.report_markdown;
}

/* A timestamp pill opens the transcript and flashes the nearest line. */
function jumpToTranscript(seconds) {
  const fold = el("meeting-transcript-fold");
  if (!fold) return;
  fold.open = true;
  const lines = [...fold.querySelectorAll(".rp__line")];
  if (!lines.length) return;
  let best = lines[0];
  for (const line of lines) {
    if (Number(line.dataset.start) <= seconds + 0.5) best = line;
    else break;
  }
  best.scrollIntoView({ behavior: "smooth", block: "center" });
  best.classList.remove("is-flash");
  void best.offsetWidth; // restart the animation
  best.classList.add("is-flash");
}

async function startMeeting() {
  const kvm = el("meeting-kvm").value;
  if (!kvm) return;
  state.meetingBusy = "start";
  renderMeetings();
  try {
    clearNotice("action");
    const job = await api("/api/meetings/start", {
      method: "POST",
      body: JSON.stringify({ kvm }),
    }).then((response) => response.json());
    announce(`Meeting recording on ${kvm} started.`);
    followJob(job, { select: true, open: true });
  } catch (error) {
    state.meetingBusy = null;
    renderMeetings();
    actionFailed(`Start recording · ${kvm}`, error);
  }
}

async function stopMeeting() {
  state.meetingBusy = "stop";
  renderMeetings();
  try {
    clearNotice("action");
    const job = await api("/api/meetings/stop", { method: "POST" }).then((response) => response.json());
    announce("Meeting recording is being stopped and processed.");
    followJob(job, { select: true, open: true });
  } catch (error) {
    state.meetingBusy = null;
    renderMeetings();
    actionFailed("Stop & process meeting", error);
  }
}

async function copyMeetingReport() {
  const text = state.meetingDetail?.report_markdown;
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    toast("success", "Report copied to the clipboard.");
  } catch {
    toast("warn", "The clipboard is unavailable here; select the report text and copy it manually.");
  }
}

function bindMeetingControls() {
  el("meeting-start")?.addEventListener("click", startMeeting);
  el("meeting-stop")?.addEventListener("click", stopMeeting);
  el("meetings-reload")?.addEventListener("click", () => loadMeetings());
  el("meeting-detail-close")?.addEventListener("click", closeMeetingDetail);
  el("meeting-copy-report")?.addEventListener("click", copyMeetingReport);
  document.addEventListener("click", (event) => {
    const jump = event.target.closest("[data-transcript-jump]");
    if (jump) {
      jumpToTranscript(Number(jump.dataset.transcriptJump));
      return;
    }
    const row = event.target.closest("[data-session-id]");
    if (row) openMeetingSession(row.dataset.sessionId);
  });
}

/* ---------------- refresh ---------------- */

function historyPath(kvm, { current = false } = {}) {
  const query = new URLSearchParams({
    days: current ? "0" : String(state.range),
    limit: current ? "1" : "300",
  });
  if (kvm) query.set("kvm", kvm);
  return `/api/history?${query}`;
}

function updateLastUpdated() {
  const target = el("last-updated");
  if (!target) return;
  if (!state.lastUpdatedAt) {
    target.textContent = "Waiting for data…";
    return;
  }
  target.textContent = `Updated ${relativeTime(state.lastUpdatedAt)}` +
    (state.lastRefreshFailures ? ` · ${state.lastRefreshFailures} source(s) unavailable` : "");
}

function renderAll() {
  syncBusy();
  renderSchedule();
  renderAlerts();
  renderFleet();
  renderOverview();
  renderAvailability();
  renderTriageButtons();
  renderTriage();
  renderAgendaButtons();
  renderAgenda();
  renderActivity();
  renderGlance();
  renderProfiles();
  renderMeetings();
  renderTaskCenter();
  updateLastUpdated();
}

async function doRefresh(generation) {
  const content = el("content");
  const refreshButton = el("refresh");
  content.setAttribute("aria-busy", "true");
  refreshButton.disabled = true;
  try {
    const requests = [
      { key: "history", label: "Selected-range history", promise: getJSON(historyPath(null)) },
      { key: "current", label: "Current availability state", promise: getJSON(historyPath(null, { current: true })) },
      { key: "schedule", label: "Scheduler state", promise: getJSON("/api/schedule") },
      { key: "jobs", label: "Task state", promise: getJSON("/api/jobs?limit=50") },
    ];
    if (state.activityKvm) {
      requests.push({
        key: "activity",
        label: `${state.activityKvm} history`,
        promise: getJSON(historyPath(state.activityKvm)),
      });
    }

    const settled = await Promise.allSettled(requests.map((request) => request.promise));
    if (generation !== state.refreshGeneration) return;
    let successes = 0;
    let failures = 0;
    settled.forEach((result, index) => {
      const request = requests[index];
      if (result.status === "fulfilled") {
        successes += 1;
        clearNotice(`refresh-${request.key}`);
        if (request.key === "history") state.fleetHistory = result.value;
        if (request.key === "current") state.currentHistory = result.value;
        if (request.key === "schedule") state.schedule = result.value;
        if (request.key === "jobs") reconcileJobList(result.value);
        if (request.key === "activity") state.activityHistory = result.value;
      } else {
        failures += 1;
        setNotice(
          `refresh-${request.key}`,
          `${request.label} could not refresh: ${result.reason.message} Previously loaded data remains visible.`,
        );
      }
    });
    if (!state.activityKvm && state.fleetHistory) state.activityHistory = state.fleetHistory;
    if (successes) state.lastUpdatedAt = new Date().toISOString();
    state.lastRefreshFailures = failures;
    renderAll();
    for (const job of state.jobs.values()) {
      if (job.status === "running" && !state.streams.has(job.id)) followJob(job);
    }
    if (failures) announce(`${plural(failures, "dashboard source")} could not refresh.`);
  } finally {
    content.removeAttribute("aria-busy");
    refreshButton.disabled = false;
  }
}

function refresh() {
  const generation = ++state.refreshGeneration;
  if (state.refreshPromise) {
    state.refreshQueued = true;
    return state.refreshPromise;
  }
  state.refreshPromise = doRefresh(generation).finally(async () => {
    state.refreshPromise = null;
    if (state.refreshQueued) {
      state.refreshQueued = false;
      await refresh();
    }
  });
  return state.refreshPromise;
}

/* ---------------- theme ---------------- */

const THEMES = ["auto", "light", "dark"];
const THEME_LABELS = { auto: "System", light: "Light", dark: "Dark" };

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  el("theme-label").textContent = THEME_LABELS[theme];
  el("theme-toggle").setAttribute("aria-label", `Theme: ${THEME_LABELS[theme]}. Switch colour theme`);
  el("theme-toggle").title = `Using ${THEME_LABELS[theme].toLowerCase()} theme`;
  try {
    localStorage.setItem("work-agent-theme", theme);
  } catch {
    /* private browsing */
  }
}

/* ---------------- boot ---------------- */

function bindChartTableToggles() {
  for (const button of document.querySelectorAll("[data-toggle-table]")) {
    const key = button.dataset.toggleTable;
    const chart = el(key);
    const table = el(`${key}-table`);
    button.setAttribute("aria-controls", `${key} ${key}-table`);
    button.setAttribute("aria-expanded", String(!table.hidden));
    button.addEventListener("click", () => {
      const showTable = table.hidden;
      table.hidden = !showTable;
      chart.hidden = showTable;
      button.textContent = showTable ? "Chart" : "Table";
      button.setAttribute("aria-expanded", String(showTable));
    });
  }
}

function applyCapabilities() {
  const sections = {
    triage: "triage",
    agenda: "agenda",
    screenshot: "screen",
    profiles: "profiles",
    meetings: "meetings",
  };
  const missing = [];
  for (const capability of ["kvm_status", "job_cancel"]) {
    if (!state.capabilities.includes(capability)) missing.push(capability);
  }
  for (const [capability, section] of Object.entries(sections)) {
    if (state.capabilities.includes(capability)) continue;
    missing.push(capability);
    const item = document.querySelector(`.navitem[data-section="${section}"]`);
    if (item) {
      item.classList.add("is-planned");
      item.disabled = true;
    }
    const option = el("section-select")?.querySelector(`option[value="${section}"]`);
    if (option) option.disabled = true;
  }
  if (missing.length) {
    setNotice(
      "capabilities",
      `This page is newer than the running dashboard server (${missing.join(", ")} unavailable). Restart it with: pikvm-agent dashboard`,
    );
  } else {
    clearNotice("capabilities");
  }
}

function populateScreenshotProfiles(config) {
  const select = el("shot-kvm");
  clear(select);
  for (const profile of readyProfiles()) {
    const option = node("option", null, profile.name);
    option.value = profile.name;
    select.append(option);
  }
  if (config.default_kvm && readyProfiles().some((profile) => profile.name === config.default_kvm)) {
    select.value = config.default_kvm;
  }
  el("shot-capture").disabled = !select.value || !state.capabilities.includes("screenshot");
  if (!select.value) el("shot-meta").textContent = "No ready KVM is available.";
}

/* /api/config lists only enabled, runnable-or-not profiles; the Profiles panel refetches it
   after every change so run targets, filters, and the schedule panel follow. */
async function loadConfig() {
  const config = await getJSON("/api/config");
  state.config = config;
  state.kvms = config.kvms;
  state.capabilities = config.capabilities || [];
  el("footer-paths").textContent =
    `log ${config.log_path} · state ${config.state_path} · schedule timezone ${config.timezone}`;
  populateScreenshotProfiles(config);
  return config;
}

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
  el("section-select")?.addEventListener("change", (event) => showSection(event.target.value));
  window.addEventListener("popstate", () => {
    showSection(sectionFromHash() || "overview", { push: false, focus: true });
  });
  bindChartTableToggles();

  el("drawer-toggle").addEventListener("click", () => {
    const body = el("drawer-body");
    body.hidden = !body.hidden;
    el("drawer-toggle").setAttribute("aria-expanded", String(!body.hidden));
    updateDrawerHeight();
  });
  if (window.ResizeObserver) {
    const observer = new ResizeObserver(updateDrawerHeight);
    observer.observe(el("drawer"));
  }

  document.addEventListener("click", (event) => {
    const goto = event.target.closest("[data-goto]");
    if (goto) {
      showSection(goto.dataset.goto);
      return;
    }
    const profileTrigger = event.target.closest("[data-profile-action][data-profile-name]");
    if (profileTrigger && !profileTrigger.disabled) {
      profileAction(profileTrigger.dataset.profileName, profileTrigger.dataset.profileAction);
      return;
    }
    const enroll = event.target.closest("[data-profile-enroll]");
    if (enroll && !enroll.disabled) {
      openTotpPanel(enroll.dataset.profileEnroll);
      return;
    }
    const quickShot = event.target.closest("[data-quick-shot]");
    if (quickShot && !quickShot.disabled) {
      quickCapture(quickShot.dataset.quickShot);
      return;
    }
    const trigger = event.target.closest("[data-action][data-kvm]");
    if (trigger && !trigger.disabled) {
      startAvailability(trigger.dataset.action, trigger.dataset.kvm);
      return;
    }
    const triage = event.target.closest("[data-triage]");
    if (triage && !triage.disabled) {
      startTriage(triage.dataset.triage);
      return;
    }
    const agenda = event.target.closest("[data-agenda]");
    if (agenda && !agenda.disabled) startAgenda(agenda.dataset.agenda);
  });

  for (const button of document.querySelectorAll("[data-schedule]")) {
    button.addEventListener("click", () => {
      const action = button.dataset.schedule;
      if (action === "uninstall") {
        inlineConfirm(el("schedule-remove-confirm"), {
          text: "Turn off and remove the three generated LaunchAgents? Nothing runs on a schedule until the scheduler is repaired.",
          confirmLabel: "Remove scheduler",
          onConfirm: () => startSchedule(action, button.dataset.availability),
        });
        return;
      }
      startSchedule(action, button.dataset.availability);
    });
  }

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || event.defaultPrevented) return;
    if (el("shot-dialog")?.open) return; // the native dialog handles its own Esc
    if (closeTransient()) event.preventDefault();
  });

  el("job-cancel")?.addEventListener("click", () => {
    if (state.selectedJob) cancelJob(state.selectedJob);
  });

  el("range-select").addEventListener("change", (event) => {
    state.range = Number(event.target.value);
    refresh();
  });
  el("refresh").addEventListener("click", refresh);
  el("triage-clear")?.addEventListener("click", clearTriage);
  el("agenda-clear")?.addEventListener("click", clearAgenda);
  el("shot-capture").addEventListener("click", captureScreenshot);
  el("shot-clear")?.addEventListener("click", () => clearScreenshot({ announceChange: true }));
  el("shot-expand")?.addEventListener("click", expandScreenshot);
  window.addEventListener("beforeunload", () => {
    for (const controller of state.streams.values()) controller.abort();
    if (state.shot.controller) state.shot.controller.abort();
    if (state.shot.url) URL.revokeObjectURL(state.shot.url);
  });

  bindProfilePanel();

  await loadConfig();
  applyCapabilities();

  renderAll();
  const initialSection = sectionFromHash() || (Object.hasOwn(PAGE_META, storedSection) ? storedSection : "overview");
  showSection(initialSection, { push: false, focus: false });
  bindMeetingControls();
  await Promise.all([refresh(), loadJobs(), loadProfiles(), loadMeetings({ quiet: true })]);
  updateDrawerHeight();

  window.setInterval(() => {
    refresh();
    updateLastUpdated();
  }, 60000);
}

boot().catch((error) => {
  setNotice("boot", `Dashboard failed to start: ${error.message}`);
  announce("Dashboard failed to start.");
});
