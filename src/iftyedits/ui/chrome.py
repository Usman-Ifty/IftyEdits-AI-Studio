"""Page chrome shared by the Streamlit views: headings that adapt to being embedded.

The studio groups several view scripts into one navigation section (for example Library and
Transcripts inside My Videos). A section page sets the embed flag while it runs a view, so the
view draws a section heading instead of a second page title. Views still work on their own.
This is the one ``ui`` module that imports Streamlit; it holds no business logic and no I/O.
"""

from __future__ import annotations

import runpy
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import NoReturn

import streamlit as st

EMBED_KEY = "_ie_embedded"
SECTION_KEY = "ce-section"
REVIEW_SECTION = "Review candidates"
EDITOR_SECTION = "Edit Short"
UPLOAD_SECTION = "Upload package"
PUBLISH_PROJECT_KEY = "publish-project"


def embedded() -> bool:
    """True while a section page is running a view inside one of its panels."""
    return bool(st.session_state.get(EMBED_KEY, False))


class ViewDone(Exception):
    """Raised by :func:`halt` inside an embedded view to end just that view."""


def halt() -> NoReturn:
    """End the current view: ``st.stop()`` on its own page, only this view when embedded.

    ``st.stop()`` would also stop the other panels of a section page, so embedded views
    raise :class:`ViewDone`, which :func:`run_view` catches.
    """
    if embedded():
        raise ViewDone
    st.stop()


def heading(title: str, caption: str = "") -> None:
    """A page title, or a smaller section heading when the view is embedded."""
    if embedded():
        st.subheader(title)
    else:
        st.title(title)
    if caption:
        st.caption(caption)


@contextmanager
def embed() -> Iterator[None]:
    """Mark views run inside this block as embedded (always cleared afterwards)."""
    st.session_state[EMBED_KEY] = True
    try:
        yield
    finally:
        st.session_state[EMBED_KEY] = False


def run_view(name: str) -> None:
    """Run ``app/views/<name>.py`` in place, as if its body were written here."""
    path = Path(__file__).resolve().parents[3] / "app" / "views" / f"{name}.py"
    with embed(), suppress(ViewDone):
        runpy.run_path(str(path), run_name=f"view_{name}")
