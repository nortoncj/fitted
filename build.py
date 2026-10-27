"""
build.py : master.yaml + preset -> DOCX + PDF, squeezed to max_pages, logged.

Usage:
    python build.py --preset devops --company "L3Harris" --url https://...
    python build.py --preset embedded --company "Honeywell" --headline "Firmware Engineer"
    python build.py --preset devops --company "Acme" --boost terraform,aws,linux
    python build.py --all                      # every preset, sanity sweep, no log
    python build.py --preset devops --company "Acme" --cover --role "Platform Engineer" \
        --manager "Jane Doe" --why-file why_acme.txt   # resume + cover letter, same folder

Output (one folder per application, recruiter-friendly filename inside):
    out/2026-10-03_l3harris_devops/Chris_Norton_DevOps_Cloud_Engineer_Resume.docx
    out/2026-10-03_l3harris_devops/Chris_Norton_DevOps_Cloud_Engineer_Resume.pdf
    out/2026-10-03_l3harris_devops/Chris_Norton_DevOps_Cloud_Engineer_Cover_Letter.pdf  (--cover)
    log.json  <- one entry per application, Doomscroll can ingest this later

Deps: pip install docxtpl pydantic pyyaml pypdf   + LibreOffice installed
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from docxtpl import DocxTemplate
import jinja2

from models import Resume, load_resume
from selector import (CoverSelection, Selection, build_context, build_cover_context,
                      next_bullet_to_drop, next_proof_to_drop)

ROOT = Path(__file__).resolve().parent
MASTER = Path(os.environ.get("RESUME_MASTER", ROOT / "master.yaml"))
TEMPLATE = Path(os.environ.get("RESUME_TEMPLATE", ROOT / "template.docx"))
COVER_TEMPLATE = Path(os.environ.get("COVER_TEMPLATE", ROOT / "cover_template.docx"))
FONTS_DIR = ROOT / "fonts"   # every font the template uses; copied into LibreOffice's profile
OUT_DIR = ROOT / "out"
LOG = ROOT / "log.json"
MAX_FIT_ITERATIONS = 15


# ---------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------
@dataclass
class BuildResult:
    preset: str
    docx: Path
    pdf: Path
    pages: int
    selection: Selection
    dropped: list[str] = field(default_factory=list)
    fits: bool = True


@dataclass
class CoverResult:
    docx: Path
    pdf: Path
    pages: int
    selection: CoverSelection
    dropped: list[int] = field(default_factory=list)   # proof indexes cut to fit
    fits: bool = True


# ---------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------
def slugify(text: str, sep: str = "-", lower: bool = True) -> str:
    """'DevOps & Cloud Engineer' -> 'devops-cloud-engineer' or 'DevOps_Cloud_Engineer'."""
    text = text.lower() if lower else text
    return re.sub(r"[^A-Za-z0-9]+", sep, text).strip(sep)


def find_soffice() -> str:
    candidates = [
        os.environ.get("SOFFICE"),
        shutil.which("soffice"),
        shutil.which("libreoffice"),
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        r"C:\Program Files\LibreOffice\program\soffice.exe",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    sys.exit("❌ LibreOffice not found. Install it or set SOFFICE=/path/to/soffice")


def page_count(pdf: Path) -> int:
    from pypdf import PdfReader
    return len(PdfReader(str(pdf)).pages)


# ---------------------------------------------------------------------
# Render + convert
# ---------------------------------------------------------------------
def render_docx(context: dict, out_path: Path, template: Path = TEMPLATE) -> Path:
    tpl = DocxTemplate(str(template))
    # autoescape: every "&" in your data would otherwise corrupt the XML.
    # StrictUndefined: a key mismatch between template and selector crashes
    # loudly instead of silently rendering an empty section.
    env = jinja2.Environment(autoescape=True, undefined=jinja2.StrictUndefined)
    tpl.render(context, jinja_env=env, autoescape=True)
    tpl.save(str(out_path))
    return out_path


def to_pdf(docx: Path, out_dir: Path) -> Path:
    # Throwaway LibreOffice profile: stops the classic hang when the
    # LibreOffice GUI is already open on your machine.
    profile = Path(tempfile.gettempdir()) / "resume_lo_profile"
    # Headless LibreOffice can't see macOS system/user fonts, so Roboto fell
    # back to Linux Libertine: wider (2 pages) and its "e" extracts as \x03,
    # which an ATS reads as garbage. It does load fonts from the profile.
    fonts = profile / "user" / "fonts"
    fonts.mkdir(parents=True, exist_ok=True)
    for f in FONTS_DIR.glob("*.[ot]tf"):
        if not (fonts / f.name).exists():
            shutil.copy(f, fonts / f.name)
    cmd = [
        find_soffice(),
        f"-env:UserInstallation={profile.as_uri()}",
        "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(docx),
    ]
    # Two soffice runs on one profile (e.g. a Streamlit rerun mid-build) make
    # the second exit 1, so serialize on a lock file and retry once.
    with open(profile / "build.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for attempt in range(2):
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if res.returncode == 0:
                break
        else:
            raise RuntimeError(
                f"LibreOffice exited {res.returncode}:\n{res.stderr or res.stdout}")
    pdf = out_dir / (docx.stem + ".pdf")
    if not pdf.exists():
        raise RuntimeError(f"LibreOffice didn't produce {pdf}")
    return pdf


# ---------------------------------------------------------------------
# Core: build one resume, squeezing to max_pages
# ---------------------------------------------------------------------
def build(
    resume: Resume,
    preset: str,
    company: str | None = None,
    out_dir: Path | None = None,
    headline: str | None = None,
    boost: set[str] | None = None,
    fit: bool = True,
) -> BuildResult:
    max_pages = resume.presets[preset].max_pages
    dropped: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for _ in range(MAX_FIT_ITERATIONS):
            sel = build_context(resume, preset, company=company, boost_tags=boost,
                                drop=set(dropped), headline=headline)
            docx = render_docx(sel.context, tmp / "resume.docx")
            pdf = to_pdf(docx, tmp)
            pages = page_count(pdf)
            if not fit or pages <= max_pages:
                break
            victim = next_bullet_to_drop(sel)
            if victim is None:
                break
            dropped.append(victim)

        # Final files: folder per application, clean filename for recruiters
        stamp = datetime.now().strftime("%Y-%m-%d")
        folder_name = f"{stamp}_{slugify(company or 'general')}_{preset}"
        dest = (out_dir or OUT_DIR) / folder_name
        n = 2
        while dest.exists():
            dest = (out_dir or OUT_DIR) / f"{folder_name}-{n}"
            n += 1
        dest.mkdir(parents=True)

        name = slugify(resume.profile.name, "_", lower=False)
        role = slugify(sel.context["headline"], "_", lower=False)
        base = f"{name}_{role}_Resume"
        final_docx = dest / f"{base}.docx"
        final_pdf = dest / f"{base}.pdf"
        shutil.copy(docx, final_docx)
        shutil.copy(pdf, final_pdf)

    return BuildResult(preset=preset, docx=final_docx, pdf=final_pdf, pages=pages,
                       selection=sel, dropped=dropped, fits=pages <= max_pages)


# ---------------------------------------------------------------------
# Cover letter: built AFTER the resume, from the same selection, into the
# same folder. Always one page; cuts lowest-priority proof to get there.
# ---------------------------------------------------------------------
def build_cover(
    resume: Resume,
    res: BuildResult,
    company: str,
    role: str | None = None,
    hiring_manager: str = "",
    company_address: str = "",
    why: str = "",
) -> CoverResult:
    now = datetime.now()
    date = f"{now:%B} {now.day}, {now.year}"
    dropped: list[int] = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for _ in range(MAX_FIT_ITERATIONS):
            sel = build_cover_context(resume, res.preset, res.selection, company, role=role,
                                      hiring_manager=hiring_manager,
                                      company_address=company_address, why=why,
                                      date=date, drop=set(dropped))
            docx = render_docx(sel.context, tmp / "cover.docx", template=COVER_TEMPLATE)
            pdf = to_pdf(docx, tmp)
            pages = page_count(pdf)
            if pages <= 1:
                break
            victim = next_proof_to_drop(sel)
            if victim is None:
                break
            dropped.append(victim)

        base = res.docx.name.removesuffix("_Resume.docx") + "_Cover_Letter"
        final_docx = res.docx.parent / f"{base}.docx"
        final_pdf = res.docx.parent / f"{base}.pdf"
        shutil.copy(docx, final_docx)
        shutil.copy(pdf, final_pdf)

    return CoverResult(docx=final_docx, pdf=final_pdf, pages=pages, selection=sel,
                       dropped=dropped, fits=pages <= 1)


def report_cover(c: CoverResult) -> None:
    icon = "✅" if c.fits else "⚠️ "
    print(f"{icon} [cover:{c.selection.letter_id}] {c.pages} page(s) -> {c.pdf}")
    if c.dropped:
        print(f"   trimmed proof paragraphs to fit: {c.dropped}")
    if not c.fits:
        print("   still over a page. Shorten the --why paragraph or lower some proof priorities.")
    for w in c.selection.warnings:
        print(f"   - {w}")


# ---------------------------------------------------------------------
# Log (atomic append so a crash can't eat your history)
# ---------------------------------------------------------------------
def append_log(res: BuildResult, company: str | None, url: str | None,
               boost: set[str] | None, cover: CoverResult | None = None) -> None:
    entries = json.loads(LOG.read_text(encoding="utf-8")) if LOG.exists() else []
    entries.append({
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "preset": res.preset,
        "headline": res.selection.context["headline"],
        "company": company,
        "url": url,
        "boost": sorted(boost or []),
        "pages": res.pages,
        "dropped": res.dropped,
        "bullets": res.selection.selected_bullet_ids,   # what actually went out
        "files": {"docx": str(res.docx), "pdf": str(res.pdf)},
        "cover": None if cover is None else {
            "letter": cover.selection.letter_id,
            "role": cover.selection.context["role"],
            "proof": [p["refs"] for p in cover.selection.proof],
            "why": cover.selection.why,
            "files": {"docx": str(cover.docx), "pdf": str(cover.pdf)},
        },
        "status": "applied",   # later: interview / rejected / ghosted
    })
    tmp = LOG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    tmp.replace(LOG)


def report(res: BuildResult) -> None:
    icon = "✅" if res.fits else "⚠️ "
    print(f"{icon} [{res.preset}] {res.pages} page(s) -> {res.pdf}")
    if res.dropped:
        print(f"   trimmed to fit: {', '.join(res.dropped)}")
    if not res.fits:
        print("   still too long and nothing droppable left. Lower max_bullets or cut a project.")
    for w in res.selection.warnings:
        print(f"   - {w}")


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a tailored, ATS-safe resume.")
    ap.add_argument("--preset", help="devops | embedded | martech | software ...")
    ap.add_argument("--company", help="used in folder name and log")
    ap.add_argument("--url", help="job posting URL, goes in the log")
    ap.add_argument("--headline", help="headline variant from master.yaml")
    ap.add_argument("--boost", help="comma separated tags from the JD, e.g. terraform,aws")
    ap.add_argument("--no-fit", action="store_true", help="skip the fit-to-page loop")
    ap.add_argument("--all", action="store_true", help="build every preset (no log)")
    ap.add_argument("--cover", action="store_true", help="also build a cover letter")
    ap.add_argument("--role", help="cover: job title as posted (default: resume headline)")
    ap.add_argument("--manager", default="", help="cover: hiring manager name (default: 'Hiring Team')")
    ap.add_argument("--address", default="", help="cover: company address line")
    why = ap.add_mutually_exclusive_group()
    why.add_argument("--why", default="", help="cover: 'why this company' paragraph")
    why.add_argument("--why-file", type=Path, help="cover: read the 'why' paragraph from a text file")
    args = ap.parse_args()

    resume = load_resume(MASTER)
    boost = {t.strip().lower() for t in args.boost.split(",")} if args.boost else None

    if args.all:
        for name in resume.presets:
            report(build(resume, name, company="sweep", fit=not args.no_fit))
        return

    if not args.preset:
        ap.error("--preset is required (or use --all)")
    if not args.company:
        ap.error("--company is required so the log means something")

    res = build(resume, args.preset, company=args.company, headline=args.headline,
                boost=boost, fit=not args.no_fit)
    report(res)

    cover = None
    if args.cover:
        why_text = args.why_file.read_text(encoding="utf-8") if args.why_file else args.why
        cover = build_cover(resume, res, args.company, role=args.role,
                            hiring_manager=args.manager, company_address=args.address,
                            why=why_text)
        report_cover(cover)
    append_log(res, args.company, args.url, boost, cover)


if __name__ == "__main__":
    main()