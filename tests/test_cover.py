"""
tests/test_cover.py : builds resume + cover letter for every preset that has
a letter, then reads the cover PDF back as plain text.

Run:  pytest -q tests/test_cover.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from docx import Document
from pypdf import PdfReader

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from build import MASTER, CoverResult, build, build_cover  # noqa: E402
from models import load_resume  # noqa: E402
from selector import build_context, build_cover_context, pick_cover_letter  # noqa: E402

RESUME = load_resume(MASTER)
COMPANY = "Zephyr Widgets"
ROLE = "Test Role Title"
WHY = "Zephyr's move to event-driven billing is the kind of migration I like to own end to end."


def has_letter(preset: str) -> bool:
    try:
        pick_cover_letter(RESUME, preset)
        return True
    except KeyError:
        return False


COVER_PRESETS = [p for p in RESUME.presets if has_letter(p)]
LETTERLESS = [p for p in RESUME.presets if not has_letter(p)]


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def pdf_text(c: CoverResult) -> str:
    return norm("\n".join(p.extract_text() or "" for p in PdfReader(str(c.pdf)).pages))


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> dict[str, CoverResult]:
    out = tmp_path_factory.mktemp("cover")
    covers = {}
    for name in COVER_PRESETS:
        res = build(RESUME, name, company=COMPANY, out_dir=out)
        covers[name] = build_cover(RESUME, res, COMPANY, role=ROLE, why=WHY)
    return covers


@pytest.mark.parametrize("preset", COVER_PRESETS)
def test_one_page(built, preset):
    assert built[preset].pages == 1, f"{preset}: cover is {built[preset].pages} pages"


@pytest.mark.parametrize("preset", COVER_PRESETS)
def test_no_leftovers(built, preset):
    """Unrendered jinja, unfilled {placeholders}, or None on the page."""
    c = built[preset]
    docx = "\n".join(p.text for p in Document(str(c.docx)).paragraphs)
    for label, text in (("pdf", pdf_text(c)), ("docx", docx)):
        for junk in ("{{", "}}", "{%", "%}", "{company}", "{role}"):
            assert junk not in text, f"{preset} {label}: found '{junk}'"
        assert not re.search(r"\bNone\b", text), f"{preset} {label}: literal 'None'"


@pytest.mark.parametrize("preset", COVER_PRESETS)
def test_company_role_and_why_on_page(built, preset):
    text = pdf_text(built[preset])
    assert COMPANY in text
    assert f"Re: {ROLE}" in text
    assert norm(WHY)[:50] in text, "why paragraph missing"
    assert "Dear Hiring Team," in text, "blank manager should fall back to 'Hiring Team'"


@pytest.mark.parametrize("preset", COVER_PRESETS)
def test_proof_backed_by_resume(built, preset):
    """Every proof paragraph must cite at least one bullet that's on the resume."""
    c = built[preset]
    assert c.selection.proof, f"{preset}: no proof paragraphs made it in"
    text = pdf_text(c)
    for p in c.selection.proof:
        assert norm(p["text"])[:50] in text, f"{preset}: proof {p['index']} not extractable"


@pytest.mark.parametrize("preset", COVER_PRESETS)
def test_filename_matches_resume(built, preset):
    c = built[preset]
    assert c.pdf.name.endswith("_Cover_Letter.pdf")
    assert (c.pdf.parent / c.pdf.name.replace("_Cover_Letter", "_Resume")).exists()


def test_proof_skipped_when_bullets_not_on_resume():
    preset = COVER_PRESETS[0]
    sel = build_context(RESUME, preset)
    sel.selected_bullet_ids = []          # pretend the resume shows nothing
    cover = build_cover_context(RESUME, preset, sel, COMPANY)
    assert cover.proof == []
    assert any("none of its bullets" in w for w in cover.warnings)


@pytest.mark.parametrize("preset", LETTERLESS)
def test_letterless_preset_fails_loud(preset):
    with pytest.raises(KeyError, match="no cover letter"):
        pick_cover_letter(RESUME, preset)
