"""The studio's look: one stylesheet and small HTML helpers (no Streamlit import, no I/O).

Colours come from ``.streamlit/config.toml``; this adds spacing, cards, status pills and a
compact layout for laptop screens. Everything user-supplied is escaped before it reaches HTML.
"""

from __future__ import annotations

from html import escape

TONES = {
    "good": ("#2DD4A0", "rgba(45, 212, 160, 0.14)"),
    "bad": ("#FF6B7A", "rgba(255, 107, 122, 0.14)"),
    "warn": ("#FFC857", "rgba(255, 200, 87, 0.14)"),
    "info": ("#5CC8FF", "rgba(92, 200, 255, 0.14)"),
    "busy": ("#A99BFF", "rgba(124, 108, 255, 0.18)"),
    "muted": ("#8C97B2", "rgba(140, 151, 178, 0.14)"),
}

CSS = """
<style>
.block-container { padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1280px; }
h1 { font-weight: 750; letter-spacing: -0.02em; }
h2, h3 { font-weight: 650; letter-spacing: -0.01em; }
[data-testid="stSidebar"] { border-right: 1px solid #243049; }
[data-testid="stMetric"] {
  background: linear-gradient(160deg, #141C30 0%, #101727 100%);
  border: 1px solid #243049; border-radius: 14px; padding: 14px 16px;
}
[data-testid="stMetricLabel"] { color: #8C97B2; }
div[data-testid="stVerticalBlockBorderWrapper"] { border-radius: 14px; }
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {
  background: linear-gradient(135deg, #7C6CFF 0%, #4C8DFF 100%); border: 0; color: #fff;
  font-weight: 650;
}
.stButton > button, .stFormSubmitButton > button { border-radius: 10px; }
.ie-hero {
  border: 1px solid #243049; border-radius: 18px; padding: 26px 30px; margin-bottom: 18px;
  background: radial-gradient(900px 260px at 0% 0%, rgba(124,108,255,.22), transparent 60%),
              radial-gradient(700px 240px at 100% 0%, rgba(76,141,255,.16), transparent 60%),
              #101727;
}
.ie-hero h1 { margin: 0 0 6px 0; font-size: 2rem; }
.ie-hero p { margin: 0; color: #B6BFD6; max-width: 46rem; }
.ie-pill {
  display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: .78rem;
  font-weight: 600; border: 1px solid transparent; white-space: nowrap;
}
.ie-card {
  border: 1px solid #243049; border-radius: 14px; padding: 14px 16px; margin-bottom: 10px;
  background: #101727;
}
.ie-card .ie-title { font-weight: 650; }
.ie-card .ie-meta { color: #8C97B2; font-size: .85rem; margin-top: 2px; }
.ie-steps { counter-reset: step; list-style: none; padding-left: 0; }
.ie-steps li {
  counter-increment: step; position: relative; padding: 4px 0 12px 44px; color: #D5DBEA;
}
.ie-steps li::before {
  content: counter(step); position: absolute; left: 0; top: 0; width: 30px; height: 30px;
  border-radius: 50%; background: rgba(124,108,255,.2); color: #C9C1FF; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
}
.ie-brand { display:flex; align-items:center; gap:10px; padding: 4px 4px 14px 4px; }
.ie-brand .ie-logo {
  width: 34px; height: 34px; border-radius: 10px; display:flex; align-items:center;
  justify-content:center; font-weight: 800; color:#fff;
  background: linear-gradient(135deg, #7C6CFF 0%, #4C8DFF 100%);
}
.ie-brand .ie-name { font-weight: 750; letter-spacing: -0.01em; line-height: 1.1; }
.ie-brand .ie-sub { color: #8C97B2; font-size: .75rem; }
.ie-journey { display:flex; flex-wrap: wrap; gap: 8px; align-items:center; margin: 4px 0 18px 0; }
.ie-journey .ie-step {
  display:flex; align-items:center; gap:8px; padding: 6px 12px; border-radius: 999px;
  border: 1px solid #243049; background:#101727; font-size:.85rem; color:#8C97B2;
}
.ie-journey .ie-step b {
  width: 22px; height: 22px; border-radius: 50%; display:inline-flex; align-items:center;
  justify-content:center; font-size:.75rem; background:#1B2540; color:#8C97B2;
}
.ie-journey .ie-step.done {
  color:#2DD4A0; border-color:#2DD4A055; background:rgba(45,212,160,.10);
}
.ie-journey .ie-step.done b { background:#2DD4A0; color:#08130F; }
.ie-journey .ie-step.next {
  color:#FFC857; border-color:#FFC85766; background:rgba(255,200,87,.10);
}
.ie-journey .ie-step.next b { background:#FFC857; color:#1A1405; }
.ie-gallery-card {
  border:1px solid #243049; border-radius:14px; padding:12px 14px; background:#101727;
  margin-bottom: 6px;
}
.ie-gallery-card .ie-title { font-weight: 650; overflow-wrap: anywhere; }
.ie-gallery-card .ie-meta { color:#8C97B2; font-size:.82rem; margin-top:2px; }
.ie-empty {
  border: 1px dashed #2F3D5E; border-radius: 14px; padding: 22px; text-align:center;
  color:#8C97B2; background: rgba(16,23,39,.6);
}
.ie-empty b { color:#E8ECF5; display:block; margin-bottom:4px; font-size:1.02rem; }
code, pre { overflow-wrap: anywhere; white-space: pre-wrap !important; }
@media (max-width: 900px) {
  .block-container { padding-left: 1rem; padding-right: 1rem; }
  .ie-hero { padding: 18px; }
  .ie-hero h1 { font-size: 1.5rem; }
}
</style>
"""


def pill(text: str, tone: str = "muted") -> str:
    """A small rounded status label as HTML."""
    colour, background = TONES.get(tone, TONES["muted"])
    return (
        f'<span class="ie-pill" style="color:{colour};background:{background};'
        f'border-color:{colour}55">{escape(text)}</span>'
    )


def hero(title: str, text: str) -> str:
    """The banner at the top of a page."""
    return f'<div class="ie-hero"><h1>{escape(title)}</h1><p>{escape(text)}</p></div>'


def card(title: str, meta: str = "", badge: str = "") -> str:
    """A compact list card; ``badge`` is trusted HTML (use :func:`pill`)."""
    meta_html = f'<div class="ie-meta">{escape(meta)}</div>' if meta else ""
    return (
        f'<div class="ie-card"><div class="ie-title">{escape(title)} {badge}</div>{meta_html}</div>'
    )


def steps(items: list[str]) -> str:
    """A numbered list of plain-text steps."""
    body = "".join(f"<li>{escape(item)}</li>" for item in items)
    return f'<ol class="ie-steps">{body}</ol>'


def clip_status(status: str) -> tuple[str, str]:
    """``(label, tone)`` for a produced clip's status."""
    return {
        "succeeded": ("Ready", "good"),
        "failed": ("Failed", "bad"),
        "skipped": ("Skipped", "warn"),
        "selected": ("Selected", "good"),
    }.get(status, (status.capitalize(), "muted"))


WORKFLOW = ("Import", "Prepare", "Transcribe", "Find clips", "Export")


def workflow_tracker(done: tuple[bool, ...]) -> str:
    """The five workflow steps as labels: finished ones ticked, the first open one marked next.

    ``done`` holds one flag per step in :data:`WORKFLOW` order. A later step is never shown as
    done before an earlier one (the flags are read left to right and stop at the first gap).
    """
    parts: list[str] = []
    gap = False
    for index, name in enumerate(WORKFLOW):
        finished = index < len(done) and done[index] and not gap
        if finished:
            parts.append(pill(f"\u2713 {name}", "good"))
        elif not gap:
            parts.append(pill(f"Next: {name}", "warn"))
            gap = True
        else:
            parts.append(pill(name, "muted"))
    return " ".join(parts)


JOURNEY = (
    "Paste link",
    "Import video",
    "Analyze",
    "Generate clips",
    "Review candidates",
    "Edit Short",
    "Export",
)


def journey(done: tuple[bool, ...]) -> str:
    """The seven-step Shorts workflow: finished steps ticked, the first open one marked next.

    ``done`` holds one flag per step in :data:`JOURNEY` order; a later step is never shown as
    done after a gap.
    """
    parts: list[str] = []
    gap = False
    for index, name in enumerate(JOURNEY):
        finished = index < len(done) and done[index] and not gap
        if finished:
            state, mark = "done", "\u2713"
        elif not gap:
            state, mark, gap = "next", str(index + 1), True
        else:
            state, mark = "", str(index + 1)
        parts.append(f'<span class="ie-step {state}"><b>{mark}</b>{escape(name)}</span>')
    return f'<div class="ie-journey">{"".join(parts)}</div>'


def brand() -> str:
    """The sidebar logo block."""
    return (
        '<div class="ie-brand"><div class="ie-logo">I</div><div>'
        '<div class="ie-name">IftyEdits</div><div class="ie-sub">AI Shorts Studio</div>'
        "</div></div>"
    )


def empty_state(title: str, text: str) -> str:
    """A friendly dashed box for pages with nothing to show yet."""
    return f'<div class="ie-empty"><b>{escape(title)}</b>{escape(text)}</div>'


def gallery_card(title: str, meta: str, badge: str = "") -> str:
    """A finished-Short card header; ``badge`` is trusted HTML (use :func:`pill`)."""
    return (
        f'<div class="ie-gallery-card"><div class="ie-title">{escape(title)}</div>'
        f'<div class="ie-meta">{escape(meta)}</div><div style="margin-top:6px">{badge}</div></div>'
    )
