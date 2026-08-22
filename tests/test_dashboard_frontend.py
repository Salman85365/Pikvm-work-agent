from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

STATIC_ROOT = Path(__file__).parents[1] / "src" / "work_agent" / "dashboard" / "static"
HTML = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
CSS = (STATIC_ROOT / "styles.css").read_text(encoding="utf-8")
JAVASCRIPT = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")


class _ElementIndex(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.by_id: dict[str, list[tuple[str, dict[str, str | None]]]] = {}

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id is not None:
            self.by_id.setdefault(element_id, []).append((tag, attributes))


def _elements() -> _ElementIndex:
    parser = _ElementIndex()
    parser.feed(HTML)
    return parser


def _element(element_id: str) -> tuple[str, dict[str, str | None]]:
    matches = _elements().by_id.get(element_id, [])
    assert len(matches) == 1, f"expected exactly one #{element_id}, found {len(matches)}"
    return matches[0]


def _function_body(name: str) -> str:
    match = re.search(
        rf"(?:async\s+)?function\s+{re.escape(name)}\([^)]*\)\s*\{{(.*?)\n\}}",
        JAVASCRIPT,
        flags=re.DOTALL,
    )
    assert match is not None, f"could not find function {name}"
    return match.group(1)


def test_hidden_content_and_navigation_keep_accessible_state() -> None:
    assert re.search(
        r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important\s*;?[^}]*\}",
        CSS,
        flags=re.DOTALL,
    )

    heading_tag, heading = _element("view-title")
    assert heading_tag == "h1"
    assert heading.get("tabindex") == "-1"

    select_tag, _ = _element("section-select")
    assert select_tag == "select"
    nav_sections = set(re.findall(r'class="navitem[^\"]*"[^>]+data-section="([^\"]+)"', HTML))
    assert nav_sections == {
        "overview",
        "availability",
        "triage",
        "agenda",
        "meetings",
        "profiles",
        "schedule",
        "activity",
        "screen",
    }
    select_start = HTML.index('id="section-select"')
    select_html = HTML[select_start : HTML.index("</select>", select_start)]
    assert set(re.findall(r'<option value="([^"]+)">', select_html)) == nav_sections
    # The URL hash selects the section, and the back button restores it.
    assert "window.location.hash" in _function_body("sectionFromHash")
    assert "window.history.pushState" in _function_body("showSection")
    assert 'window.addEventListener("popstate"' in JAVASCRIPT
    assert 'item.setAttribute("aria-current", "page")' in JAVASCRIPT
    assert 'item.removeAttribute("aria-current")' in JAVASCRIPT
    assert "select.value = name" in _function_body("showSection")


def test_task_updates_use_a_concise_announcer_and_quiet_trace() -> None:
    announcer_tag, announcer = _element("task-announcer")
    assert announcer_tag == "div"
    assert announcer.get("role") == "status"
    assert announcer.get("aria-live") == "polite"
    assert announcer.get("aria-atomic") == "true"

    trace_tag, trace = _element("trace")
    assert trace_tag == "pre"
    assert "aria-live" not in trace
    assert 'trace.removeAttribute("aria-live")' in JAVASCRIPT
    assert 'trace.setAttribute("role", "log")' in JAVASCRIPT

    announce_body = _function_body("announce")
    assert "region.textContent = message" in announce_body
    assert 'el("trace")' not in announce_body


def test_small_screens_have_touch_targets_and_card_style_tables() -> None:
    touch_media = re.search(
        r"@media\s*\(max-width:\s*700px\)\s*\{(.*?)"
        r"@media\s*\(max-width:\s*680px\)",
        CSS,
        flags=re.DOTALL,
    )
    assert touch_media is not None
    touch_rules = touch_media.group(1)
    assert ".mobile-nav { display: grid" in touch_rules
    assert ".card__head { flex-direction: column" in touch_rules
    assert re.search(
        r"\.button,\s*\.ghost-button,.*?\.linkish\s*\{\s*min-height:\s*44px",
        touch_rules,
        flags=re.DOTALL,
    )

    card_media = re.search(
        r"@media\s*\(max-width:\s*680px\)\s*\{(.*?)"
        r"@media\s*\(max-width:\s*480px\)",
        CSS,
        flags=re.DOTALL,
    )
    assert card_media is not None
    card_rules = card_media.group(1)
    assert ".table--responsive tbody tr" in card_rules
    assert "display: grid" in card_rules
    assert ".table--responsive td::before" in card_rules
    assert "overflow: visible" in card_rules


def test_schedule_leads_with_the_human_rule_and_keeps_diagnostics_secondary() -> None:
    readable = " ".join(re.sub(r"<[^>]+>", " ", HTML).split())
    assert "Active every Monday\N{EN DASH}Friday at 18:00" in readable
    assert "Away at 02:00 the following morning" in readable

    sync_position = HTML.index('id="schedule-sync"')
    diagnostics_position = HTML.index('id="schedule-diagnostics"')
    diagnostics_end = HTML.index("</details>", diagnostics_position)
    assert sync_position < diagnostics_position

    diagnostics_tag, _ = _element("schedule-diagnostics")
    assert diagnostics_tag == "details"
    for diagnostic_id in (
        "sched-interp-status",
        "sched-interp",
        "agent-list",
        "schedule-repair",
        "schedule-remove",
    ):
        assert diagnostics_position < HTML.index(f'id="{diagnostic_id}"') < diagnostics_end


def test_schedule_is_green_only_when_current_state_matches_the_rule() -> None:
    body = _function_body("scheduleStatus")

    assert "currentStateFor(profile.name).value !== snapshot.desired_now" in body
    assert "environmentsNeedingVerification === 0" in body
    assert "Schedule needs verification" in body
    assert '"Schedule verified"' in body
    assert '"Scheduler ready"' not in body
    assert "scheduleStatus(snapshot)" in _function_body("renderSchedule")


def test_remote_frame_is_explicitly_temporary_and_user_clearable() -> None:
    readable = " ".join(re.sub(r"<[^>]+>", " ", HTML).split())
    assert "Observe only · never written to disk" in readable
    assert "One live PiKVM frame" in readable
    assert "Capture one temporary, read-only PiKVM frame." in JAVASCRIPT

    for button_id in ("shot-capture", "shot-clear", "shot-expand"):
        tag, _ = _element(button_id)
        assert tag == "button"
    dialog_tag, dialog = _element("shot-dialog")
    assert dialog_tag == "dialog"
    assert dialog.get("aria-labelledby") == "shot-dialog-title"
    image_tag, image = _element("shot-dialog-image")
    assert image_tag == "img"
    assert "hidden" in image


def test_current_state_refresh_is_independent_from_the_history_range() -> None:
    assert "currentHistory: null" in JAVASCRIPT
    assert "outcomeFor(kvm, state.currentHistory)" in _function_body("currentStateFor")

    history_path = _function_body("historyPath")
    assert 'days: current ? "0" : String(state.range)' in history_path
    assert 'limit: current ? "1" : "300"' in history_path

    refresh_body = _function_body("doRefresh")
    assert 'key: "history"' in refresh_body
    assert 'key: "current"' in refresh_body
    assert "historyPath(null, { current: true })" in refresh_body
    assert "state.currentHistory = result.value" in refresh_body


def test_jobs_have_independent_recoverable_streams_and_cursor_resume() -> None:
    assert re.search(r"jobs:\s*new Map\(\)", JAVASCRIPT)
    assert re.search(r"streams:\s*new Map\(\)", JAVASCRIPT)
    follow_job = _function_body("followJob")
    assert "state.streams.has(merged.id)" in follow_job
    assert "state.streams.set(merged.id, controller)" in follow_job
    assert "const eventCursor = merged.events?.length || 0" in follow_job
    assert "/events?after=${eventCursor}" in follow_job
    assert "state.streams.delete(merged.id)" in follow_job

    load_jobs = _function_body("loadJobs")
    assert 'getJSON("/api/jobs?limit=50")' in load_jobs
    assert 'if (job.status === "running") followJob(job)' in load_jobs


def test_task_center_compacts_consecutive_identical_stops() -> None:
    compact = _function_body("compactJobs")
    fingerprint = _function_body("failedJobFingerprint")
    render = _function_body("renderTaskCenter")

    assert 'job.status !== "failed"' in fingerprint
    assert "previous?.fingerprint === fingerprint" in compact
    assert "previous.repeatCount += 1" in compact
    assert "groupedIds: [job.id]" in compact
    assert "compactJobs(sortedJobs())" in render
    task_row = _function_body("taskRow")
    assert "identical stopped attempts" in task_row
    assert "repetitionLabel" in task_row
    assert 'row.setAttribute(\n    "aria-label"' in task_row


def test_agenda_replaces_stale_results_and_rejects_late_older_jobs() -> None:
    assert "agendaPending: false" in JAVASCRIPT
    assert "agendaSourceJob: null" in JAVASCRIPT
    assert "agendaSourceStartedAt: null" in JAVASCRIPT

    start = _function_body("startAgenda")
    current = _function_body("agendaJobIsCurrent")
    pending = _function_body("markAgendaPending")
    absorb = _function_body("absorbJobPayload")
    render = _function_body("renderAgenda")

    assert "markAgendaPending(job)" in start
    assert "incoming >= current" in current
    assert "state.agenda = null" in pending
    assert "state.agendaCapturedAt = null" in pending
    assert "state.agendaPending = true" in pending
    assert "agendaJobIsCurrent(job)" in absorb
    assert "job.payload || failedAgendaPayload(job)" in absorb
    assert "state.agendaPending = false" in absorb
    assert '"Reading now…"' in render
    assert "`Checked ${localTime(state.agendaCapturedAt)}" in render


def test_refresh_guard_and_temporary_frame_cleanup_are_retained() -> None:
    assert "Promise.allSettled" in _function_body("doRefresh")
    refresh_body = _function_body("refresh")
    assert "if (state.refreshPromise)" in refresh_body
    assert "return state.refreshPromise" in refresh_body
    assert "state.refreshPromise = doRefresh(generation).finally" in refresh_body
    assert "state.refreshQueued = true" in refresh_body
    assert "state.refreshPromise = null" in refresh_body

    assert re.search(r"const SCREEN_RETENTION_MS\s*=\s*5\s*\*\s*60\s*\*\s*1000", JAVASCRIPT)
    clear_body = _function_body("clearScreenshot")
    retain_body = _function_body("retainScreenshot")
    assert "URL.revokeObjectURL(state.shot.url)" in clear_body
    assert "URL.revokeObjectURL(state.shot.url)" in retain_body
    assert "state.shot.controller.abort()" in clear_body
    assert "window.setTimeout" in retain_body
    assert "SCREEN_RETENTION_MS" in retain_body
    assert "Frame cleared automatically after 5 minutes." in retain_body


def test_fleet_failure_banner_and_liveness_are_rendered_from_the_schedule_snapshot() -> None:
    alerts_tag, alerts = _element("fleet-alerts")
    assert alerts_tag == "div"
    assert alerts.get("role") == "alert"
    assert "hidden" in alerts

    render_alerts = _function_body("renderAlerts")
    assert "state.schedule?.kvms" in render_alerts
    assert "item.alert" in render_alerts
    assert "alert.label" in render_alerts
    assert "relativeTime(alert.since)" in render_alerts
    assert "alert.reason" in render_alerts
    assert "renderAlerts()" in _function_body("renderAll")

    liveness = _function_body("livenessChips")
    assert "status.workflow_running" in liveness
    assert "A scheduled/CLI workflow is running now" in liveness
    reach = _function_body("reachabilityChip")
    assert "Unreachable since" in reach
    assert "status.unreachable_since" in reach
    assert "livenessChips(kvmStatusFor(profile.name))" in _function_body("renderFleet")


def test_history_rows_show_telemetry_numbers_only() -> None:
    assert HTML.count('<th scope="col">Effort</th>') == 2
    row = _function_body("historyRow")
    assert "telemetryText(record.telemetry)" in row
    telemetry = _function_body("telemetryText")
    assert "telemetry.steps" in telemetry
    assert "telemetry.hid_actions" in telemetry
    assert "telemetry.total_tokens" in telemetry
    assert "telemetry.runtime_seconds" in telemetry
    assert '.table--history td:nth-child(7)::before { content: "Effort"; }' in CSS
    assert "cell.colSpan = 7" in _function_body("renderOverview")
    assert "cell.colSpan = 7" in _function_body("renderActivity")


def test_schedule_panel_shows_last_run_and_per_environment_outcomes() -> None:
    for element_id in ("sched-last-run", "sched-fleet"):
        assert element_id in _elements().by_id
    body = _function_body("renderSchedule")
    assert "snapshot.last_run" in body
    assert "snapshot.kvms || []" in body
    assert "Schedule needs attention" in _function_body("scheduleStatus")
    readable = " ".join(re.sub(r"<[^>]+>", " ", HTML).split())
    assert "RunAtLoad" in readable
    assert "can be cancelled from the task center" in readable


def test_task_center_can_cancel_an_interruptible_job_and_handles_pruned_streams() -> None:
    cancel_tag, _ = _element("job-cancel")
    assert cancel_tag == "button"
    controls_tag, controls = _element("task-controls")
    assert controls_tag == "div"
    assert "hidden" in controls

    render = _function_body("renderTaskCenter")
    assert "selected.cancellable" in render
    assert 'state.capabilities.includes("job_cancel")' in render
    assert "selected?.cancel_requested" in render
    cancel_body = _function_body("cancelJob")
    assert "/cancel" in cancel_body
    assert 'method: "POST"' in cancel_body
    follow = _function_body("followJob")
    assert 'frame.event === "gone"' in follow
    assert "state.jobs.delete(merged.id)" in follow
    assert '"cancelled"' in _function_body("jobStatusChip")
    assert 'if (job.status === "cancelled") return "Cancelled"' in _function_body("jobStatusLabel")
    capabilities = _function_body("applyCapabilities")
    assert '"kvm_status"' in capabilities and '"job_cancel"' in capabilities


def test_profiles_panel_manages_named_pikvms_without_ever_showing_a_secret() -> None:
    assert 'data-section="profiles"' in HTML
    assert '"profiles"' in _function_body("applyCapabilities")
    assert "profiles: null" in JAVASCRIPT

    form_tag, _ = _element("profile-form")
    assert form_tag == "form"
    _, password = _element("profile-password")
    assert password.get("type") == "password"
    assert password.get("autocomplete") == "new-password"
    _, name = _element("profile-name")
    assert name.get("pattern") == "[a-z0-9][a-z0-9_\\-]{0,39}"
    _, totp = _element("profile-totp")
    assert "checked" in totp
    _, verify = _element("profile-verify-ssl")
    assert "checked" not in verify
    assert re.search(
        r"const PROFILE_NAME_PATTERN = /\^\[a-z0-9\]\[a-z0-9_-\]\{0,39\}\$/", JAVASCRIPT
    )

    submit = _function_body("submitProfileForm")
    assert 'api("/api/profiles"' in submit
    assert "totp_required: totpRequired" in submit
    assert "form.reset()" in submit
    assert 'el("profile-password").value = ""' in submit
    assert "showFormError(error.message)" in submit

    row = _function_body("profileRow")
    assert "password" not in row.lower()
    assert "seed" not in row.lower()
    assert 'make(card.enabled ? "disable" : "enable"' in row
    assert 'make("test", "Test connection")' in row
    assert "if (card.removable)" in row
    assert "defined in .env — disable instead" in row
    assert 'row.classList.add("is-disabled")' in row
    assert "reachabilityChip(live)" in row

    chips = _function_body("profileChips")
    assert '"2FA enrolled"' in chips
    assert '"2FA not enrolled"' in chips
    assert '"No 2FA"' in chips
    assert '"Enroll 2FA"' in chips

    action = _function_body("profileAction")
    assert "inlineConfirm(host" in action
    assert "window.confirm(" not in JAVASCRIPT.replace("never window.confirm().", "")
    assert "state.profileBusy.add(key)" in action
    assert "afterProfileChange()" in action
    after = _function_body("afterProfileChange")
    assert "await loadConfig()" in after
    assert "await loadProfiles()" in after


def test_totp_enrolment_uploads_raw_bytes_and_offers_an_inline_replace() -> None:
    _, file_input = _element("totp-file")
    assert file_input.get("type") == "file"
    assert file_input.get("accept") == "image/png,image/jpeg"
    _, zone = _element("totp-dropzone")
    assert zone.get("role") == "button"
    assert zone.get("tabindex") == "0"
    for button_id in ("totp-replace-confirm", "totp-replace-cancel", "totp-close"):
        tag, _ = _element(button_id)
        assert tag == "button"
    readable = " ".join(re.sub(r"<[^>]+>", " ", HTML).split())
    assert "locally on this Mac" in readable
    assert "macOS Keychain" in readable

    enroll = _function_body("enrollTotp")
    assert "await file.arrayBuffer()" in enroll
    assert "/totp?replace=${replace" in enroll
    assert '"Content-Type": file.type' in enroll
    assert "/already exists/i.test(error.message)" in enroll
    assert 'el("totp-replace").hidden = false' in enroll
    assert "result.notes" in enroll
    assert "URL.createObjectURL" not in enroll
    accept = _function_body("acceptableQrFile")
    assert '"image/png", "image/jpeg"' in accept
    assert "8 * 1024 * 1024" in accept
    bind = _function_body("bindProfilePanel")
    assert '"drop"' in bind
    assert "enrollTotp(state.totp.file, { replace: true })" in bind


def test_overview_cards_offer_quick_actions_and_esc_closes_transient_layers() -> None:
    fleet = _function_body("renderFleet")
    assert "triage.dataset.triage = profile.name" in fleet
    assert "shot.dataset.quickShot = profile.name" in fleet
    assert 'state.capabilities.includes("screenshot")' in fleet
    quick = _function_body("quickCapture")
    assert 'showSection("screen"' in quick
    assert "captureScreenshot()" in quick
    close = _function_body("closeTransient")
    assert ".confirm:not([hidden])" in close
    assert "closeTotpPanel()" in close
    assert 'el("drawer-body")' in close
    assert 'event.key !== "Escape"' in JAVASCRIPT


def test_meetings_panel_drives_the_recorder_and_shows_reports_without_secrets() -> None:
    assert 'data-section="meetings"' in HTML
    assert '"meetings"' in _function_body("applyCapabilities")
    for element_id in (
        "meeting-kvm",
        "meeting-start",
        "meeting-stop",
        "sessions-list",
        "meeting-detail",
        "meeting-copy-report",
        "meeting-setup",
    ):
        _element(element_id)
    assert 'api("/api/meetings/start"' in _function_body("startMeeting")
    assert 'api("/api/meetings/stop"' in _function_body("stopMeeting")
    assert "/api/meetings/sessions/" in _function_body("openMeetingSession")
    # Live recordings re-poll themselves and stop polling when idle.
    ticker = _function_body("syncMeetingTicker")
    assert "setInterval" in ticker and "clearInterval" in ticker
    # Report viewer groups action items by ownership and keeps the transcript folded.
    detail = _function_body("renderMeetingDetail")
    assert "our_identity" in detail and "possibly_our_identity" in detail
    assert "rp__transcript" in detail
    # Every category is visually distinguished: stats strip, per-kind hue, timestamp jump pills.
    assert "rp__stats" in detail
    assert '"blockers"' in detail and '"questions"' in detail and '"followups"' in detail
    assert "data-transcript-jump" in JAVASCRIPT
    jump = _function_body("jumpToTranscript")
    assert "scrollIntoView" in jump and "is-flash" in jump
    for kind in ("ours", "possible", "decision", "blocker", "question", "followup"):
        assert f'[data-kind="{kind}"]' in CSS
    # Setup problems name the env var to fix, never a key value.
    render = _function_body("renderMeetings")
    assert "DEEPGRAM_API_KEY" in render and "WORK_IDENTITY_NAME" in render
    assert "api_key" not in render
