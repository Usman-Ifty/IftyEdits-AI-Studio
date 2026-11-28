"""Display helpers for the Upload package page (no I/O)."""

from __future__ import annotations

from collections.abc import Iterable

from iftyedits.core import readiness as rd

TAB_ORDER = ("Rights", "Metadata", "Safety review", "Edit Short", "Checklist")
"""Where open work is done, in the order it is easiest to do it."""


def tab_for(item: rd.Item) -> str:
    """The place on screen where one checklist item is completed."""
    if item.key in rd.USER_CHECKS or item.key in ("audience", "visibility", "rights_ack"):
        return "Checklist"
    if item.key in ("risk_reviewed", "flags_resolved"):
        return "Safety review"
    if item.group == "Rights":
        return "Rights"
    if item.group == "Export":
        return "Edit Short"
    return "Metadata"


def remaining_steps(items: Iterable[rd.Item]) -> list[tuple[str, list[str]]]:
    """Open required items grouped by where to do them, in working order."""
    found: dict[str, list[str]] = {}
    for item in items:
        if item.required and not item.done:
            found.setdefault(tab_for(item), []).append(item.label)
    return [(tab, found[tab]) for tab in TAB_ORDER if tab in found]
