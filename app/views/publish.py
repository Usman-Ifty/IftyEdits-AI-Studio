"""Upload package: metadata, thumbnail, content review, rights and the readiness checklist.

One Short at a time. Nothing here uploads, schedules or publishes anything.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace

import streamlit as st

from iftyedits.config import Settings
from iftyedits.core import metadata as md
from iftyedits.core import readiness as rd
from iftyedits.core import rights as rt
from iftyedits.core import safety as sf
from iftyedits.core.errors import IftyEditsError
from iftyedits.services.publishing import PublishingService, build_publishing_service
from iftyedits.services.shorts import build_short_service
from iftyedits.services.upload_package import (
    build_package,
    description_with_hashtags,
    write_package,
)
from iftyedits.storage.db import open_database
from iftyedits.storage.shorts import ShortRepository
from iftyedits.storage.videos import VideoRepository
from iftyedits.ui import chrome, theme
from iftyedits.ui.formatting import utc_short
from iftyedits.ui.shorts import clock

settings = Settings.from_env()
PROJECT_KEY = "publish-project"

chrome.heading(
    "Upload package",
    "Prepare the title, description, tags, thumbnail and checks for one finished Short. "
    "Nothing is uploaded, scheduled or published from here: you copy the package into "
    "YouTube yourself.",
)


def _flash() -> None:
    message = st.session_state.pop("publish-flash", None)
    if message:
        st.success(message)


def _run(
    action: Callable[[PublishingService], object], ok_message: str, *, rerun: bool = False
) -> None:
    """Run a service call; show a clear error instead of a stack trace.

    Button callbacks already trigger a rerun. Plain ``if st.button`` code passes ``rerun=True``.
    """
    try:
        # Button callbacks run on the next rerun, after the page's own connection was closed,
        # so every action opens (and closes) its own connection.
        with closing(open_database(settings.db_path)) as own:
            action(build_publishing_service(settings, own))
    except IftyEditsError as exc:
        st.session_state["publish-error"] = str(exc)
    else:
        st.session_state["publish-flash"] = ok_message
    if rerun:
        st.rerun()


with closing(open_database(settings.db_path)) as conn:
    shorts = ShortRepository(conn)
    projects = shorts.list_projects()
    if not projects:
        st.markdown(
            theme.empty_state(
                "No Shorts to prepare yet",
                "Open Create Shorts or the Clip Editor, make a Short and export it first.",
            ),
            unsafe_allow_html=True,
        )
        chrome.halt()
    videos = {v.id: v for v in VideoRepository(conn).list_recent(limit=100_000)}
    ids = [p.id for p in projects]
    if st.session_state.get(PROJECT_KEY) not in ids:
        st.session_state[PROJECT_KEY] = ids[0]

    def _name(project_id: int) -> str:
        p = next(x for x in projects if x.id == project_id)
        video = videos.get(p.video_id)
        return f"{p.title} · {clock(p.config.start_s)} to {clock(p.config.end_s)} · " + (
            video.original_filename if video else f"video #{p.video_id}"
        )

    pid: int = st.selectbox("Short", ids, format_func=_name, key=PROJECT_KEY)
    service: PublishingService = build_publishing_service(settings, conn)
    project = service.project(pid)
    ctx = service.context(pid)
    report = service.readiness(pid)
    stored = service.metadata(pid)
    meta = stored.metadata if stored else md.Metadata()
    prov = stored.provenance if stored else {}

    error = st.session_state.pop("publish-error", None)
    if error:
        st.error(error)
    _flash()

    head = st.columns([3, 2])
    with head[0]:
        st.markdown(theme.pill(report.label, report.tone), unsafe_allow_html=True)
        if report.status == rd.READY:
            st.caption(rd.READY_MEANING)
    with head[1]:
        if service.model is None:
            st.markdown(theme.pill("AI not configured", "muted"), unsafe_allow_html=True)
            st.caption("Writing and review by hand still work. Settings has the AI setup.")
        else:
            st.markdown(theme.pill(f"AI: {service.model.name}", "accent"), unsafe_allow_html=True)

    if not ctx.has_speech:
        st.warning(
            "This clip has no transcript. Generated text can only guess from the title, so "
            "check everything yourself. Transcribe the video in My Videos for better results."
        )

    tabs = st.tabs(["Metadata", "Thumbnail", "Safety review", "Rights", "Checklist", "Package"])

    # ------------------------------------------------------------------ metadata
    with tabs[0]:
        if stored and not service.metadata_is_current(pid):
            st.warning(
                "The clip changed after this metadata was written. Check it still fits, then "
                "confirm or regenerate."
            )
            st.button(
                "It still fits this clip",
                key=f"pm-accept-{pid}",
                on_click=lambda: _run(
                    lambda svc: svc.accept_metadata_for_current_clip(pid), "Metadata kept."
                ),
            )
        cols = st.columns([2, 3])
        with cols[0]:
            if st.button(
                "Generate everything",
                key=f"pm-gen-{pid}",
                type="primary",
                icon=":material/auto_awesome:",
                disabled=service.model is None,
            ):
                with st.spinner("Writing metadata from the clip transcript..."):
                    result = service.generate_metadata(pid)
                st.session_state["publish-gen"] = result
                st.rerun()
        with cols[1]:
            picks = st.multiselect(
                "Regenerate only",
                list(md.GROUPS),
                format_func=lambda g: md.GROUP_LABELS[g],
                key=f"pm-pick-{pid}",
                disabled=service.model is None,
            )
            if st.button(
                "Regenerate selected",
                key=f"pm-regen-{pid}",
                disabled=service.model is None or not picks,
            ):
                with st.spinner("Regenerating..."):
                    result = service.generate_metadata(pid, picks)
                st.session_state["publish-gen"] = result
                st.rerun()
        if service.model is None:
            st.info(
                "No AI model is set up, so nothing can be generated. Write the fields below "
                "yourself, or add a Claude key or a local model in Settings."
            )
        outcome = st.session_state.pop("publish-gen", None)
        if outcome is not None:
            (st.success if outcome.ok else st.error)(outcome.message)
            for note in outcome.notes:
                st.caption(f"Note: {note}")
        if stored and stored.model:
            st.caption(
                f"AI-written fields come from {stored.model} and may be wrong. Review each "
                "one. Fields you edit are marked as yours."
            )

        st.markdown("#### Title")
        if meta.titles:
            labels = [
                f"{t.text}  ·  {md.TITLE_STYLES.get(t.style, t.style or 'option')}"
                + ("  ★ recommended" if i == meta.recommended_index else "")
                for i, t in enumerate(meta.titles)
            ]
            choice = st.radio(
                "Suggested titles",
                range(len(labels)),
                format_func=lambda i: labels[i],
                key=f"pm-titles-{pid}",
                index=None,
            )
            picked = meta.titles[choice].text if isinstance(choice, int) else ""
            if picked and picked != meta.selected_title:
                st.button(
                    "Use this title",
                    key=f"pm-usetitle-{pid}",
                    on_click=lambda: _run(
                        lambda svc: svc.edit_metadata(pid, selected_title=picked),
                        "Title chosen.",
                    ),
                )
        title = st.text_input(
            "Title for YouTube",
            value=meta.selected_title,
            key=f"pm-title-{pid}-{hash(meta.selected_title) & 0xFFFF}",
            max_chars=md.TITLE_MAX + 50,
        )
        errors, warnings = md.title_issues(title)
        st.caption(f"{len(title)}/{md.TITLE_MAX} characters")
        for line in errors:
            st.error(line)
        for line in warnings:
            st.warning(line)

        st.markdown("#### Description")
        description = st.text_area(
            "Description",
            value=meta.description,
            height=140,
            key=f"pm-desc-{pid}-{hash(meta.description) & 0xFFFF}",
        )
        hashtags_text = st.text_area(
            "Hashtags (one per line, kept separate from search tags)",
            value="\n".join(meta.hashtags),
            height=90,
            key=f"pm-hash-{pid}-{hash(meta.hashtags) & 0xFFFF}",
        )
        tags_text = st.text_area(
            "Search tags (one per line)",
            value="\n".join(meta.tags),
            height=110,
            key=f"pm-tags-{pid}-{hash(meta.tags) & 0xFFFF}",
        )
        clean_tags = md.clean_tags(tags_text.splitlines())
        st.caption(f"{md.tags_length(clean_tags)}/{md.TAGS_TOTAL_MAX} characters of tags")
        d_err, d_warn = md.description_issues(
            description, md.clean_hashtags(hashtags_text.splitlines())
        )
        for line in d_err:
            st.error(line)
        for line in d_warn:
            st.warning(line)

        if st.button("Save my edits", key=f"pm-save-{pid}", type="primary", icon=":material/save:"):
            changes: dict[str, object] = {}
            if title != meta.selected_title:
                changes["selected_title"] = title
            if description != meta.description:
                changes["description"] = description
            if md.clean_hashtags(hashtags_text.splitlines()) != meta.hashtags:
                changes["hashtags"] = hashtags_text.splitlines()
            if clean_tags != meta.tags:
                changes["tags"] = tags_text.splitlines()
            if changes:
                _run(lambda svc: svc.edit_metadata(pid, **changes), "Saved.", rerun=True)
            else:
                st.info("Nothing changed.")

        if meta.filled:
            rights_now = service.rights(project.video_id)
            attribution = rights_now.attribution_text or rt.generate_attribution(rights_now)
            preview = description_with_hashtags(meta, attribution, rights_now.source_url)
            st.markdown("#### Copy")
            st.caption("Each box has a copy button in its top-right corner.")
            st.code(meta.selected_title or "(no title yet)", language="text", wrap_lines=True)
            st.caption("Description as it will be pasted (includes credit and hashtags)")
            st.code(preview, language="text", wrap_lines=True)
            st.code(" ".join(meta.hashtags) or "(no hashtags)", language="text")
            st.code(", ".join(meta.tags) or "(no tags)", language="text", wrap_lines=True)

            with st.expander("More from the generator"):
                for label, value in (
                    ("Summary", meta.summary),
                    ("Topic", meta.topic),
                    ("Category", meta.category),
                    ("Audience", meta.audience),
                    ("Language", meta.language),
                    ("Confidence", meta.confidence),
                    ("Pinned comment", meta.pinned_comment),
                    ("Call to action", meta.cta),
                    ("Long description", meta.description_long),
                ):
                    if value:
                        st.markdown(f"**{label}**")
                        st.text(value)
                if meta.hooks:
                    st.markdown("**Hook lines**")
                    st.text("\n".join(meta.hooks))
                if meta.search_phrases:
                    st.markdown("**Search phrases**")
                    st.text("\n".join(meta.search_phrases))
                ai_fields = sorted(k for k, v in prov.items() if v == "ai")
                if ai_fields:
                    st.caption("Written by AI: " + ", ".join(ai_fields))

    # ------------------------------------------------------------------ thumbnail
    with tabs[1]:
        st.caption(
            "Pick a real frame from this clip. YouTube Shorts thumbnails come from the video "
            "itself, so this is a plan, not a separate image file."
        )
        duration = max(0.5, ctx.duration_s)
        default_at = (
            min(meta.thumbnail_frame_s, duration) if meta.thumbnail_frame_s is not None else 0.0
        )
        at = st.slider(
            "Frame (seconds from the start of the clip)",
            0.0,
            float(duration),
            float(default_at),
            0.1,
            key=f"pt-at-{pid}",
        )
        if st.button("Show this frame", key=f"pt-show-{pid}", icon=":material/image:"):
            try:
                with closing(open_database(settings.db_path)) as c2:
                    path = build_short_service(settings, c2).still(
                        project.video_id, project.config.start_s + at
                    )
                st.session_state[f"pt-img-{pid}"] = (str(path), at)
            except IftyEditsError as exc:
                st.error(f"Could not read that frame: {exc}")
        shown = st.session_state.get(f"pt-img-{pid}")
        if shown:
            st.image(shown[0], caption=f"Frame at {shown[1]:.1f}s", width=240)
        text = st.text_input(
            "Thumbnail text (short)", value=meta.thumbnail_text, key=f"pt-text-{pid}", max_chars=40
        )
        concept = st.text_area(
            "Concept", value=meta.thumbnail_concept, key=f"pt-concept-{pid}", height=80
        )
        composition = st.text_area(
            "Composition notes", value=meta.thumbnail_composition, key=f"pt-comp-{pid}", height=80
        )
        if st.button("Save thumbnail plan", key=f"pt-save-{pid}", type="primary"):
            _run(
                lambda svc: svc.edit_metadata(
                    pid,
                    thumbnail_frame_s=at,
                    thumbnail_text=text,
                    thumbnail_concept=concept,
                    thumbnail_composition=composition,
                ),
                "Thumbnail plan saved.",
                rerun=True,
            )
        if meta.thumbnail_frame_s is not None:
            st.caption(f"Saved frame: {meta.thumbnail_frame_s:.1f}s into the clip.")

    # ------------------------------------------------------------------ safety
    with tabs[2]:
        st.caption(sf.DISCLAIMER)
        review = service.repo.latest_review(pid)
        if st.button(
            "Run content review",
            key=f"ps-run-{pid}",
            type="primary",
            icon=":material/policy:",
        ):
            with st.spinner("Reviewing the transcript..."):
                service.run_review(pid)
            st.rerun()
        if review is None:
            st.markdown(
                theme.empty_state(
                    "Not reviewed yet",
                    "Run the review, or record that you reviewed the clip yourself.",
                ),
                unsafe_allow_html=True,
            )
        else:
            current = service.review_is_current(pid, review)
            st.markdown(
                theme.pill(sf.RISK_LABELS[review.risk_level], sf.RISK_TONES[review.risk_level])
                + (" " + theme.pill("Out of date: the clip changed", "warn") if not current else "")
                + " "
                + theme.pill(f"Run {utc_short(review.created_at)} UTC", "muted"),
                unsafe_allow_html=True,
            )
            if review.status == sf.STATUS_UNAVAILABLE:
                st.warning(review.note)
            elif review.status == sf.STATUS_FAILED:
                st.error(f"The review could not finish: {review.note}")
            elif review.status == sf.STATUS_MANUAL:
                st.info(f"You recorded a manual review: {review.note}")
            elif review.summary:
                st.write(review.summary)
            for note in review.notes:
                st.caption(note)
            resolutions = service.repo.resolutions(pid)
            for flag in review.flags:
                res = resolutions.get(flag.key)
                where = (
                    f"{flag.start_s:.1f}s to {flag.end_s:.1f}s into the clip"
                    if flag.start_s is not None and flag.end_s is not None
                    else "position unknown"
                )
                title_text = f"{sf.CATEGORIES.get(flag.category, flag.category)} · {flag.severity}"
                with st.container(border=True):
                    st.markdown(f"**{title_text}** · {where}")
                    st.markdown(
                        theme.pill(
                            "From the transcript" if flag.certainty == sf.EVIDENCE else "Inferred",
                            "muted",
                        )
                        + (
                            " "
                            + theme.pill(sf.RESOLUTION_LABELS.get(res.action, res.action), "accent")
                            if res
                            else " " + theme.pill("Open", "warn")
                        ),
                        unsafe_allow_html=True,
                    )
                    if flag.excerpt:
                        st.code(flag.excerpt, language="text", wrap_lines=True)
                    st.write(flag.explanation)
                    if res and res.note:
                        st.caption(f"Your note: {res.note}")
                    note = st.text_input(
                        "Note (required to dismiss)", key=f"ps-note-{pid}-{flag.key}"
                    )
                    row = st.columns(3)
                    row[0].button(
                        "Dismiss as false positive",
                        key=f"ps-dis-{pid}-{flag.key}",
                        on_click=lambda k=flag.key, n=note: _run(
                            lambda svc: svc.resolve_flag(pid, k, sf.RESOLUTION_DISMISSED, n),
                            "Dismissed. The record is kept.",
                        ),
                    )
                    row[1].button(
                        "Acknowledge, still a concern",
                        key=f"ps-ack-{pid}-{flag.key}",
                        on_click=lambda k=flag.key, n=note: _run(
                            lambda svc: svc.resolve_flag(pid, k, sf.RESOLUTION_ACKNOWLEDGED, n),
                            "Acknowledged.",
                        ),
                    )
                    if res:
                        row[2].button(
                            "Reopen",
                            key=f"ps-reopen-{pid}-{flag.key}",
                            on_click=lambda k=flag.key: _run(
                                lambda svc: svc.resolve_flag(pid, k, "reopened", ""), "Reopened."
                            ),
                        )
        st.markdown("#### Review it yourself")
        manual = st.text_area(
            "What did you check?", key=f"ps-manual-{pid}", height=80, max_chars=500
        )
        st.button(
            "Record my manual review",
            key=f"ps-manual-btn-{pid}",
            on_click=lambda: _run(
                lambda svc: svc.record_manual_review(pid, manual), "Manual review recorded."
            ),
        )
        history = service.repo.reviews(pid)
        if len(history) > 1:
            with st.expander(f"Earlier reviews ({len(history) - 1})"):
                for old in history[1:]:
                    st.write(
                        f"{utc_short(old.created_at)} UTC · {sf.RISK_LABELS[old.risk_level]} · "
                        f"{len(old.flags)} flag(s)"
                    )

    # ------------------------------------------------------------------ rights
    with tabs[3]:
        st.caption(rt.NOT_LEGAL_ADVICE)
        video = videos.get(project.video_id)
        st.markdown(
            f"**Source:** {video.original_filename if video else project.video_id}. "
            "This record belongs to the source video, so every Short cut from it shares it."
        )
        saved = service.rights(project.video_id)
        hint = service.site_license_hint(project.video_id)
        if hint:
            st.info(hint)
        vid = project.video_id
        status_keys = list(rt.STATUSES)
        status = st.selectbox(
            "Permission status",
            status_keys,
            index=status_keys.index(saved.status) if saved.status in status_keys else 0,
            format_func=lambda k: rt.STATUSES[k],
            key=f"pr-status-{vid}",
        )
        if status in rt.EVIDENCE_REQUIRED:
            st.caption("This status needs evidence below: a license link, a message or a note.")
        rc1, rc2 = st.columns(2)
        source_title = rc1.text_input("Source title", saved.source_title, key=f"pr-title-{vid}")
        creator = rc2.text_input("Creator / channel", saved.creator, key=f"pr-creator-{vid}")
        owner = rc1.text_input("Rights owner (if different)", saved.owner, key=f"pr-owner-{vid}")
        source_url = rc2.text_input("Source URL", saved.source_url, key=f"pr-url-{vid}")
        license_type = rc1.text_input("License name", saved.license_type, key=f"pr-lic-{vid}")
        license_url = rc2.text_input("License link", saved.license_url, key=f"pr-licurl-{vid}")
        evidence = st.text_area(
            "Evidence (what shows you may use it)", saved.evidence, key=f"pr-ev-{vid}", height=90
        )
        notes = st.text_area("Permission notes", saved.permission_notes, key=f"pr-notes-{vid}")
        tri = list(rt.TRISTATE)
        d1, d2, d3 = st.columns(3)
        attribution_required = d1.selectbox(
            "Credit required",
            tri,
            index=tri.index(saved.attribution_required),
            format_func=lambda k: rt.TRISTATE[k],
            key=f"pr-attr-{vid}",
        )
        commercial = d2.selectbox(
            "Commercial use allowed",
            tri,
            index=tri.index(saved.commercial_use),
            format_func=lambda k: rt.TRISTATE[k],
            key=f"pr-comm-{vid}",
        )
        derivatives = d3.selectbox(
            "Edits / derivatives allowed",
            tri,
            index=tri.index(saved.derivatives_allowed),
            format_func=lambda k: rt.TRISTATE[k],
            key=f"pr-deriv-{vid}",
        )
        attribution_text = st.text_input(
            "Credit line", saved.attribution_text, key=f"pr-credit-{vid}"
        )
        expires = st.text_input("Permission expires on (YYYY-MM-DD, optional)", saved.expires_on)
        confirmed = st.checkbox(
            "I reviewed these details myself and they are accurate to the best of my knowledge",
            value=saved.user_confirmed,
            key=f"pr-conf-{vid}",
        )
        draft = replace(
            saved,
            status=status,
            source_title=source_title,
            creator=creator,
            owner=owner,
            source_url=source_url,
            license_type=license_type,
            license_url=license_url,
            evidence=evidence,
            permission_notes=notes,
            attribution_required=attribution_required,
            commercial_use=commercial,
            derivatives_allowed=derivatives,
            attribution_text=attribution_text,
            expires_on=expires,
        )
        suggestion = rt.generate_attribution(draft)
        if suggestion and not attribution_text:
            st.caption(f"Suggested credit line from what you entered: {suggestion}")
        if st.button("Save rights record", key=f"pr-save-{vid}", type="primary"):
            _run(
                lambda svc: svc.save_rights(vid, draft, confirmed=confirmed),
                "Rights record saved.",
                rerun=True,
            )
        if saved.reviewed_on:
            st.caption(f"Last confirmed by you on {saved.reviewed_on}.")
        changes_log = service.repo.rights_history(vid)
        if changes_log:
            with st.expander(f"History ({len(changes_log)})"):
                for entry in reversed(changes_log):
                    names = ", ".join(entry.changes) or "created"
                    st.write(
                        f"{utc_short(entry.created_at)} UTC · "
                        f"{rt.STATUSES.get(entry.old_status or '', 'none')} → "
                        f"{rt.STATUSES.get(entry.new_status, entry.new_status)} · changed: {names}"
                    )

    # ------------------------------------------------------------------ checklist
    with tabs[4]:
        st.caption(rd.READY_MEANING)
        st.markdown("#### Your choices for the upload")
        st.caption(
            "These are your decisions. The app only reminds you; it never picks the audience."
        )
        current_settings, ack = service.choices(pid)
        aud_keys = [k for k in rd.AUDIENCES if k]
        a1, a2 = st.columns(2)
        audience = a1.radio(
            "Is this video made for kids?",
            aud_keys,
            index=aud_keys.index(current_settings["audience"])
            if current_settings.get("audience") in aud_keys
            else None,
            format_func=lambda k: rd.AUDIENCES[k],
            key=f"pc-aud-{pid}",
        )
        vis_keys = [k for k in rd.VISIBILITIES if k]
        visibility = a2.radio(
            "Visibility you plan to use",
            vis_keys,
            index=vis_keys.index(current_settings["visibility"])
            if current_settings.get("visibility") in vis_keys
            else None,
            format_func=lambda k: rd.VISIBILITIES[k],
            key=f"pc-vis-{pid}",
        )
        if audience and audience != current_settings.get("audience"):
            service.set_setting(pid, "audience", audience)
            st.rerun()
        if visibility and visibility != current_settings.get("visibility"):
            service.set_setting(pid, "visibility", visibility)
            st.rerun()
        for advice in (meta.comments_advice, meta.visibility_advice):
            if advice:
                st.caption(f"Suggestion (AI): {advice}")
        if meta.disclosure_notes:
            st.caption("Disclosure reminders (AI): " + "; ".join(meta.disclosure_notes))

        st.markdown("#### Things only you can confirm")
        facts = service.facts(pid)
        for key, label in rd.USER_CHECKS.items():
            ticked = st.checkbox(label, value=key in facts.checks, key=f"pc-{key}-{pid}")
            if ticked != (key in facts.checks):
                service.set_check(pid, key, ticked)
                st.rerun()
        unverified = facts.rights_status in rt.UNVERIFIED or facts.rights_status in (
            rt.CREATIVE_COMMONS,
            rt.PUBLIC_DOMAIN,
        )
        if unverified:
            acked = st.checkbox(
                "I understand the rights for this source are not verified, and I accept "
                "responsibility for that",
                value=ack,
                key=f"pc-ack-{pid}",
            )
            if acked != ack:
                service.set_rights_ack(pid, acked)
                st.rerun()
            st.caption("This acknowledgement does not clear the source. It is only recorded.")

        st.markdown("#### Checklist")
        group = ""
        for item in report.items:
            if item.group != group:
                group = item.group
                st.markdown(f"**{group}**")
            icon = "✅" if item.done else ("⛔" if item.blocking else "⬜")
            tail = "" if item.done else f" — {item.detail}" if item.detail else ""
            st.write(f"{icon} {item.label}{tail}")
        for line in report.blockers:
            st.error(line)
        for line in report.caveats:
            st.warning(line)
        if report.revoked:
            st.warning("Something changed after you confirmed, so that confirmation was withdrawn.")

        st.markdown(theme.pill(report.label, report.tone), unsafe_allow_html=True)
        if report.status == rd.READY:
            st.button(
                "Withdraw my confirmation",
                key=f"pc-withdraw-{pid}",
                on_click=lambda: _run(
                    lambda svc: svc.withdraw_confirmation(pid), "Confirmation withdrawn."
                ),
            )
        else:
            st.button(
                "Confirm: ready for me to upload",
                key=f"pc-confirm-{pid}",
                type="primary",
                disabled=not report.can_confirm,
                help="Available once every required item above is done.",
                on_click=lambda: _run(lambda svc: svc.confirm_ready(pid), "Confirmed."),
            )
        events = service.repo.events(pid, 8)
        if events:
            with st.expander("Recent activity"):
                for name, detail, when in events:
                    st.write(f"{utc_short(when)} UTC · {name.replace('_', ' ')} · {detail}")

    # ------------------------------------------------------------------ package
    with tabs[5]:
        package = build_package(service, pid)
        st.caption(
            "Everything above in one file. Saving creates a new file each time and never "
            "replaces an earlier one."
        )
        if report.status != rd.READY:
            st.warning(f"Status: {report.label}. You can still save a draft package.")
        pk1, pk2 = st.columns(2)
        for col, fmt in ((pk1, "json"), (pk2, "txt")):
            with col:
                if st.button(
                    f"Save package as .{fmt}",
                    key=f"pk-save-{fmt}-{pid}",
                    icon=":material/download:",
                ):
                    try:
                        path = write_package(service, pid, fmt)
                    except IftyEditsError as exc:
                        st.error(str(exc))
                    else:
                        st.session_state[f"pk-saved-{pid}"] = str(path)
        saved_path = st.session_state.get(f"pk-saved-{pid}")
        if saved_path:
            st.success("Saved to:")
            st.code(saved_path, language="text", wrap_lines=True)
        st.download_button(
            "Download JSON",
            json.dumps(package, indent=2, ensure_ascii=False),
            file_name=f"upload-package-{pid}.json",
            mime="application/json",
            key=f"pk-dl-{pid}",
        )
        with st.expander("Preview"):
            st.json(package, expanded=False)
