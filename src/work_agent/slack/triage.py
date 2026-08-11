from __future__ import annotations

from work_agent.slack.triage_models import (
    AttentionLevel,
    ConversationKind,
    SlackTriagePerception,
    TriageItem,
)

TRIAGE_PROMPT = """You are reading a PiKVM screenshot of a desktop that may be running Slack.

Report only Slack's unread state as it is already visible. Read the conversation sidebar: channel
and direct-message names, unread badges, mention badges, and bold/unread styling.

Do not report message text, message previews, or conversation contents even if some are visible on
screen. Do not infer anything about what a message says. Only the conversation name, its kind, its
unread count, whether it carries a mention badge, and whether it is muted are wanted.

Set slack_foreground false when Slack is not the visible foreground application, and
sidebar_visible false when the conversation sidebar cannot be read. Report conversations only when
the sidebar is genuinely visible. Set sidebar_truncated true when the list is scrolled or clipped so
that further unread entries may exist below the visible area.

An unread count you cannot read exactly should be 0 with has_mention set from the badge. Prefer
lowering confidence over guessing a name.

Set safe_to_read false with a stop_reason for an authentication prompt, a lock screen, a
disconnected or blank feed, or any state where the sidebar cannot be trusted."""


TRIAGE_CONTEXT = (
    "Report Slack's currently visible unread conversations. Do not report message contents."
)


FOREGROUND_OBJECTIVE = """Make Slack the visible foreground application, then finish.

Prefer the Slack icon in the macOS Dock or Windows taskbar. If no icon is visible, use deterministic
OS application search. The icon may carry an unread badge; that is still the right target. Never
click inside the Slack window itself to focus it, even when part of that window is already visible.

Finish as soon as Slack's main window and its conversation sidebar are visible. If Slack is already
foreground, finish immediately without any action.

Do not open, select, or click any conversation, channel, direct message, or thread. Opening a
conversation marks it read and destroys the unread state this workflow exists to report. Do not
read, summarise, or act on any message. Do not send anything. Do not change availability, status
text, emoji, or preferences."""


def classify(conversation_kind: ConversationKind, *, has_mention: bool) -> AttentionLevel:
    """Rank a visible unread entry without reading it.

    Sidebar state supports only three honest tiers. Anything finer - whether a message is a
    question, a blocker, or an approval request - requires opening the conversation, which this
    workflow will not do.
    """

    if has_mention:
        return AttentionLevel.MENTIONED
    if conversation_kind in {ConversationKind.DIRECT_MESSAGE, ConversationKind.GROUP_MESSAGE}:
        return AttentionLevel.DIRECT
    return AttentionLevel.UNREAD


_ATTENTION_ORDER = {
    AttentionLevel.MENTIONED: 0,
    AttentionLevel.DIRECT: 1,
    AttentionLevel.UNREAD: 2,
}


def build_items(perception: SlackTriagePerception) -> tuple[TriageItem, ...]:
    items = [
        TriageItem(
            name=conversation.name.strip(),
            kind=conversation.kind,
            unread_count=conversation.unread_count,
            has_mention=conversation.has_mention,
            attention=classify(conversation.kind, has_mention=conversation.has_mention),
        )
        for conversation in perception.conversations
        if conversation.name.strip() and not conversation.muted
    ]
    items.sort(key=lambda item: (_ATTENTION_ORDER[item.attention], -item.unread_count, item.name))
    return tuple(items)
