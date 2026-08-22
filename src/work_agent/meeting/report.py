from __future__ import annotations

import html
import re

from work_agent.meeting.models import (
    ActionItem,
    BlockerRisk,
    Decision,
    FollowUp,
    MeetingIntelligence,
    MeetingMetadata,
    OpenQuestion,
    OwnerCategory,
    Reference,
    Transcript,
)

_MARKDOWN_PUNCTUATION = re.compile(r"([\\`*_{}\[\]<>#])")


def render_meeting_report(
    metadata: MeetingMetadata,
    transcript: Transcript,
    intelligence: MeetingIntelligence,
) -> str:
    """Render one deterministic Markdown report from validated local models."""

    speaker_labels = {speaker.id: speaker.label for speaker in transcript.speakers}
    segment_sources = {
        segment.id: f"[{_timestamp(segment.start_seconds)}] {speaker_labels[segment.speaker_id]}"
        for segment in transcript.segments
    }
    our_actions = [
        item
        for item in intelligence.action_items
        if item.owner_category is OwnerCategory.OUR_IDENTITY
    ]
    possible_actions = [
        item
        for item in intelligence.action_items
        if item.owner_category is OwnerCategory.POSSIBLY_OUR_IDENTITY
    ]
    other_actions = [
        item
        for item in intelligence.action_items
        if item.owner_category
        not in {OwnerCategory.OUR_IDENTITY, OwnerCategory.POSSIBLY_OUR_IDENTITY}
    ]

    lines = [
        "# Meeting report",
        "",
        "## MEETING",
        "",
        f"- KVM: {_inline(metadata.kvm)}",
        f"- Start: {metadata.started_at.isoformat()}",
        f"- End: {metadata.ended_at.isoformat()}",
        f"- Duration: {_duration(metadata.duration_seconds)}",
        f"- Capture: {'interrupted' if metadata.interrupted else 'completed'}",
        "",
        "## TRANSCRIPT",
        "",
    ]
    if transcript.segments:
        for segment in transcript.segments:
            lines.append(
                f"[{_timestamp(segment.start_seconds)}] "
                f"{speaker_labels[segment.speaker_id]}: {_inline(segment.text)}"
            )
    else:
        lines.append("_No speech was transcribed._")

    lines.extend(
        [
            "",
            "## SUMMARY",
            "",
            _inline(intelligence.summary),
            "",
            "## OUR ACTION ITEMS",
            "",
            *_action_lines(our_actions, segment_sources),
            "",
            "## POSSIBLE OUR ACTION ITEMS",
            "",
            *_action_lines(possible_actions, segment_sources),
            "",
            "## OTHER ACTION ITEMS",
            "",
            *_action_lines(other_actions, segment_sources),
            "",
            "## DECISIONS",
            "",
            *_finding_lines(intelligence.decisions, segment_sources),
            "",
            "## BLOCKERS / RISKS",
            "",
            *_risk_lines(intelligence.blockers_and_risks, segment_sources),
            "",
            "## OPEN QUESTIONS",
            "",
            *_question_lines(intelligence.open_questions, segment_sources),
            "",
            "## REFERENCES",
            "",
            *_reference_lines(intelligence.references, segment_sources),
            "",
            "## FOLLOW-UPS",
            "",
            *_follow_up_lines(intelligence.follow_ups, segment_sources),
            "",
        ]
    )
    return "\n".join(lines)


def _action_lines(
    items: list[ActionItem],
    sources: dict[str, str],
) -> list[str]:
    if not items:
        return ["_None recorded._"]
    lines: list[str] = []
    for item in items:
        lines.extend(
            [
                f"- {_inline(item.task)}",
                f"  - Owner: {_inline(item.owner) if item.owner else 'Unknown'}",
                f"  - Owner classification: {item.owner_category.value}",
                f"  - Confidence: {item.confidence.value}",
            ]
        )
        if item.requested_by:
            lines.append(f"  - Requested by: {_inline(item.requested_by)}")
        if item.due_text:
            lines.append(f"  - Due: {_inline(item.due_text)}")
        if item.reason:
            lines.append(f"  - Reason: {_inline(item.reason)}")
        lines.append(f"  - Evidence: {_sources(item.evidence_segment_ids, sources)}")
    return lines


def _finding_lines(items: list[Decision], sources: dict[str, str]) -> list[str]:
    if not items:
        return ["_None recorded._"]
    return [
        f"- {_inline(item.text)} — {item.confidence.value}; "
        f"evidence: {_sources(item.evidence_segment_ids, sources)}"
        for item in items
    ]


def _risk_lines(items: list[BlockerRisk], sources: dict[str, str]) -> list[str]:
    if not items:
        return ["_None recorded._"]
    lines: list[str] = []
    for item in items:
        owner = f"; owner: {_inline(item.owner)}" if item.owner else ""
        lines.append(
            f"- {item.kind.value.title()}: {_inline(item.text)} — "
            f"{item.confidence.value}{owner}; "
            f"evidence: {_sources(item.evidence_segment_ids, sources)}"
        )
    return lines


def _question_lines(items: list[OpenQuestion], sources: dict[str, str]) -> list[str]:
    if not items:
        return ["_None recorded._"]
    lines: list[str] = []
    for item in items:
        target = f"; directed to: {_inline(item.directed_to)}" if item.directed_to else ""
        lines.append(
            f"- {_inline(item.question)} — {item.confidence.value}{target}; "
            f"evidence: {_sources(item.evidence_segment_ids, sources)}"
        )
    return lines


def _reference_lines(items: list[Reference], sources: dict[str, str]) -> list[str]:
    if not items:
        return ["_None recorded._"]
    lines: list[str] = []
    for item in items:
        context = f" — {_inline(item.context)}" if item.context else ""
        lines.append(
            f"- {item.kind.value}: {_inline(item.value)}{context}; "
            f"evidence: {_sources(item.evidence_segment_ids, sources)}"
        )
    return lines


def _follow_up_lines(items: list[FollowUp], sources: dict[str, str]) -> list[str]:
    if not items:
        return ["_None recorded._"]
    lines: list[str] = []
    for item in items:
        owner = _inline(item.owner) if item.owner else "Unknown"
        reason = f"; reason: {_inline(item.reason)}" if item.reason else ""
        lines.append(
            f"- {_inline(item.text)} — owner: {owner} ({item.owner_category.value}); "
            f"confidence: {item.confidence.value}{reason}; "
            f"evidence: {_sources(item.evidence_segment_ids, sources)}"
        )
    return lines


def _sources(ids: list[str], sources: dict[str, str]) -> str:
    return ", ".join(sources[item_id] for item_id in ids)


def _timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    whole_seconds, fraction = divmod(milliseconds, 1000)
    minutes, second = divmod(whole_seconds, 60)
    hour, minute = divmod(minutes, 60)
    base = f"{hour:02d}:{minute:02d}:{second:02d}"
    return f"{base}.{fraction:03d}" if fraction else base


def _duration(seconds: float) -> str:
    rounded = max(0, round(seconds))
    minutes, second = divmod(rounded, 60)
    hour, minute = divmod(minutes, 60)
    parts: list[str] = []
    if hour:
        parts.append(f"{hour}h")
    if minute or hour:
        parts.append(f"{minute}m")
    parts.append(f"{second}s")
    return " ".join(parts)


def _inline(value: str) -> str:
    flattened = " ".join(value.split())
    escaped_html = html.escape(flattened, quote=False)
    return _MARKDOWN_PUNCTUATION.sub(r"\\\1", escaped_html)
