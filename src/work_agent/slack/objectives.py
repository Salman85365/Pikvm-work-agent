from __future__ import annotations

from work_agent.slack.models import Availability

_BOUNDARY = """This workflow controls only Slack's manual Active/Away availability.
Do not read or send messages. Do not edit status text, status emoji, notifications, or preferences.
Do not simulate activity or keep the computer awake. Use only verified, visible GUI navigation.
At every observation, return the next visible workflow control as a relevant clickable element:
- when Slack is not foreground, a visible Slack icon in the macOS Dock or Windows taskbar;
- when Slack is foreground, its visible profile/avatar control;
- when the profile menu is open, the manual Active/Away toggle.
The profile target must be the full clickable account avatar/button that opens Slack's profile menu.
Place its click point well inside the center of that full control, never on the smaller
presence/status badge or a neighboring toolbar control. If that full profile control cannot be
located reliably, do not invent or guess one.
If Slack has no visible Dock/taskbar icon, deterministic OS application search may be used to open
it. Before finishing, the profile menu must visibly show the manual availability toggle as a
relevant element. Interpret 'Set yourself as away' as currently Active and 'Set yourself as active'
as currently Away. Never infer availability from the objective or from an action that was merely
attempted. Opening Slack and opening its profile menu are navigation/read-only actions. Selecting
the manual availability toggle is a local edit. For each proposal, describe only its immediate
visibly verifiable expected outcome: Slack becomes foreground after opening it; the profile menu and
manual toggle become visible after opening the profile control; and the menu visibly closes or the
toggle visibly changes after selecting availability. After that immediate effect is verified,
reopen the profile menu and verify the final Active/Away state before finishing."""


def availability_objective(desired: Availability | None) -> str:
    if desired is None:
        task = (
            "Determine the current Slack manual availability. Finish only when the profile menu "
            "visibly establishes whether it is Active or Away. Do not change it."
        )
    else:
        opposite_toggle = (
            "Set yourself as away" if desired is Availability.ACTIVE else "Set yourself as active"
        )
        task = (
            f"Set Slack manual availability to {desired.value}. First visibly determine the "
            "current availability from the profile menu. If it is already "
            f"{desired.value}, finish without selecting the availability toggle. Otherwise select "
            "the appropriate manual availability toggle, then reopen the profile menu if needed "
            f"and finish only when '{opposite_toggle}' is visibly present, proving the current "
            f"availability is {desired.value}."
        )
    return f"{task}\n\n{_BOUNDARY}"
