"""
app.py : Streamlit front end for build.py (resume + optional cover letter).

Usage:
    pip install streamlit
    streamlit run app.py

Same pipeline as the CLI: build() -> build_cover() -> append_log().
The preview pane uses selector.build_context only, so it's instant and
never touches LibreOffice; the Build button runs the full fit loop.
"""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from build import LOG, MASTER, append_log, build, build_cover
from selector import build_context, pick_cover_letter, preview

st.set_page_config(page_title="Resume Builder", page_icon="📄", layout="wide")


@st.cache_resource
def load(mtime: float):
    """Reloads master.yaml whenever the file changes on disk."""
    from models import load_resume
    return load_resume(MASTER)


try:
    resume = load(MASTER.stat().st_mtime)
except Exception as e:  # validation errors list every problem; show them all
    st.error(f"master.yaml failed validation:\n\n```\n{e}\n```")
    st.stop()

all_tags = sorted({t.lower() for b in resume.bullets.values() for t in b.tags})

# ---------------------------------------------------------------------
# Sidebar: the same inputs as the CLI flags
# ---------------------------------------------------------------------
with st.sidebar:
    st.header("Application")
    preset = st.selectbox("Preset", list(resume.presets))
    p = resume.presets[preset]
    company = st.text_input("Company *")
    url = st.text_input("Job posting URL")
    variants = resume.headlines[p.headline]
    default_variant = variants.index(p.headline_variant) if p.headline_variant in variants else 0
    headline = st.selectbox("Headline", variants, index=default_variant)
    boost = set(st.multiselect("Boost tags (from the JD)", all_tags))
    fit = st.toggle("Fit to page", value=True, help=f"Target: {p.max_pages} page(s)")

    st.header("Cover letter")
    want_cover = st.toggle("Also build a cover letter")
    if want_cover:
        try:
            st.caption(f"Template: `{pick_cover_letter(resume, preset).id}`")
        except KeyError as e:
            st.warning(str(e))
            want_cover = False
    role = st.text_input("Role as posted", placeholder=headline, disabled=not want_cover)
    manager = st.text_input("Hiring manager", placeholder="Hiring Team", disabled=not want_cover)
    address = st.text_input("Company address", disabled=not want_cover)

# ---------------------------------------------------------------------
# Main area
# ---------------------------------------------------------------------
tab_build, tab_log = st.tabs(["Build", "Log"])

with tab_build:
    left, right = st.columns([3, 2])

    with left:
        st.subheader("Preview")
        sel = build_context(resume, preset, company=company or None,
                            boost_tags=boost, headline=headline)
        st.code(preview(sel), language=None)
        st.caption(f"{len(sel.selected_bullet_ids)} bullets selected before the fit loop")
        for w in sel.warnings:
            st.warning(w)

    with right:
        if want_cover:
            st.subheader("Why this company")
            why = st.text_area("The paragraph recruiters actually read", height=220,
                               label_visibility="collapsed",
                               placeholder="Write this fresh for every job.")
            if not why.strip():
                st.info("Without a 'why' paragraph the letter is generic.")
        else:
            why = ""

        if st.button("Build", type="primary", width="stretch", disabled=not company):
            try:
                with st.status("Building…", expanded=True) as status:
                    st.write("Rendering resume and fitting to page…")
                    res = build(resume, preset, company=company, headline=headline,
                                boost=boost or None, fit=fit)
                    cover = None
                    if want_cover:
                        st.write("Rendering cover letter…")
                        cover = build_cover(resume, res, company, role=role or None,
                                            hiring_manager=manager, company_address=address,
                                            why=why)
                    append_log(res, company, url or None, boost or None, cover)
                    status.update(label="Done", state="complete")
                st.session_state.last = (res, cover)
            except SystemExit as e:   # find_soffice() exits when LibreOffice is missing
                st.error(str(e))
            except Exception as e:
                st.exception(e)
        if not company:
            st.caption("Company is required so the log means something.")

        # Results persist across reruns (download clicks rerun the script)
        if "last" in st.session_state:
            res, cover = st.session_state.last
            st.divider()
            (st.success if res.fits else st.warning)(
                f"Resume: {res.pages} page(s) → `{res.pdf.parent.name}/`")
            if res.dropped:
                st.caption(f"Trimmed to fit: {', '.join(res.dropped)}")
            c1, c2 = st.columns(2)
            c1.download_button("Resume PDF", res.pdf.read_bytes(), res.pdf.name, "application/pdf")
            c2.download_button("Resume DOCX", res.docx.read_bytes(), res.docx.name)

            if cover:
                (st.success if cover.fits else st.warning)(
                    f"Cover letter ({cover.selection.letter_id}): {cover.pages} page(s)")
                if cover.dropped:
                    st.caption(f"Proof paragraphs cut to fit: {cover.dropped}")
                for w in cover.selection.warnings:
                    st.warning(w)
                c1, c2 = st.columns(2)
                c1.download_button("Cover PDF", cover.pdf.read_bytes(), cover.pdf.name,
                                   "application/pdf")
                c2.download_button("Cover DOCX", cover.docx.read_bytes(), cover.docx.name)

with tab_log:
    if LOG.exists():
        entries = json.loads(LOG.read_text(encoding="utf-8"))
        df = pd.DataFrame(entries)
        df["cover"] = df["cover"].notna() if "cover" in df else False   # older entries predate it
        cols = ["timestamp", "company", "preset", "headline", "pages", "cover", "status", "url"]
        st.dataframe(df[[c for c in cols if c in df]].iloc[::-1], hide_index=True,
                     width="stretch",
                     column_config={"url": st.column_config.LinkColumn("url")})
    else:
        st.info("No applications logged yet.")
