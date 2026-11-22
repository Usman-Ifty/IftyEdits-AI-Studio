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
    }.get(status, (status.capitalize(), "muted"))
