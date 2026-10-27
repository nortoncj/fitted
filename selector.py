"""
selector.py : preset in, render-ready context out.

NOT named select.py on purpose: that shadows Python's stdlib `select` module,
which subprocess imports, which build.py needs for LibreOffice. Ask me how I know.

Pure functions, no IO besides the __main__ preview. build.py and the Streamlit
app both call build_context().

Usage:
    python selector.py devops                 # plain text preview in terminal
    python selector.py devops --boost terraform,aws,linux
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

from models import Bullet, CoverLetter, Project, Resume, Role, load_resume

DATE_SEP = " – "
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ---------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------
@dataclass
class Selection:
    context: dict                          # hand straight to docxtpl
    warnings: list[str] = field(default_factory=list)
    selected_bullet_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def fmt_month(ym: str | None) -> str:
    if ym is None:
        return "Present"
    year, month = ym.split("-")
    return f"{MONTHS[int(month) - 1]} {year}"


def fmt_range(start: str, end: str | None) -> str:
    return f"{fmt_month(start)}{DATE_SEP}{fmt_month(end)}"


def recency_key(item: Role | Project) -> tuple[str, str]:
    """Reverse chronological: ongoing first, then latest end, then latest start."""
    return (item.end or "9999-12", item.start)


def pick_bullets(
    bullets: list[Bullet],
    tracks: set[str],
    primary: str,
    limit: int,
    boost: set[str],
    drop: set[str],
    owner: str,
    warnings: list[str],
) -> list[tuple[Bullet, int]]:
    eligible = [
        (i, b) for i, b in enumerate(bullets)
        if tracks & set(b.tracks) and b.id not in drop
    ]
    prio = {b.id: b.priority_for(primary, tracks) for _, b in eligible}

    def score(b: Bullet) -> int:
        return len({t.lower() for t in b.tags} & boost)

    # priority 1 first, then JD keyword overlap, then priority, then YAML order
    eligible.sort(key=lambda ib: (prio[ib[1].id] != 1, -score(ib[1]), prio[ib[1].id], ib[0]))
    chosen = [(b, prio[b.id]) for _, b in eligible[:limit]]

    cut_p1 = [b.id for _, b in eligible[limit:] if prio[b.id] == 1]
    if cut_p1:
        warnings.append(f"{owner}: priority-1 bullets cut by the cap: {', '.join(cut_p1)}")
    if not chosen:
        warnings.append(f"{owner}: zero bullets match this preset's tracks")
    return chosen


def bullet_dict(b: Bullet, priority: int) -> dict:
    return {"id": b.id, "text": b.text, "priority": priority}


# ---------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------
def build_context(
    resume: Resume,
    preset_name: str,
    company: str | None = None,
    boost_tags: set[str] | None = None,
    drop: set[str] | None = None,
    headline: str | None = None,
) -> Selection:
    """
    boost_tags: lowercase tags from a JD match; floats matching bullets up.
    drop:       bullet ids to exclude; the fit-to-one-page loop feeds this.
    headline:   pick a variant to mirror the posting's title. Must already
                exist in headlines[preset.headline]; no freestyling titles.
    """
    if preset_name not in resume.presets:
        raise KeyError(f"no preset '{preset_name}'. Have: {', '.join(resume.presets)}")

    p = resume.presets[preset_name]
    options = resume.headlines[p.headline]
    chosen_headline = headline or p.headline_variant or options[0]
    if chosen_headline not in options:
        raise ValueError(
            f"headline '{chosen_headline}' isn't in headlines['{p.headline}']. "
            f"Options: {', '.join(options)}. Add it to master.yaml first if it's legit."
        )
    tracks = set(p.tracks)
    boost = {t.lower() for t in (boost_tags or set())}
    drop = drop or set()
    warnings: list[str] = []

    # ---- header -------------------------------------------------------
    prof = resume.profile
    contact = [prof.location, prof.phone, prof.email]
    contact += [v for v in (prof.links.website, prof.links.linkedin, prof.links.github) if v]

    # ---- experience ---------------------------------------------------
    roles = sorted((resume.role_by_id[r] for r in p.roles), key=recency_key, reverse=True)
    experience = []
    for r in roles:
        title = r.title
        if p.show_display_note and r.display_note:
            title = f"{r.title} ({r.display_note})"
        chosen = pick_bullets(r.bullets, tracks, p.headline, p.max_bullets_per_role,
                              boost, drop, f"role '{r.id}'", warnings)
        experience.append({
            "id": r.id,
            "title": title,
            "company": r.company,
            "dates": fmt_range(r.start, r.end),
            "bullets": [bullet_dict(b, prio) for b, prio in chosen],
        })

    # ---- projects -----------------------------------------------------
    # Projects render in PRESET order (most relevant first), not by date.
    # List your strongest project first; the fit loop trims from the bottom.
    projs = [resume.project_by_id[x] for x in p.projects]
    projects = []
    for pr in projs:
        subtitle = pr.subtitle.get(p.headline) or next(
            (pr.subtitle[t] for t in p.tracks if t in pr.subtitle), None)
        chosen = pick_bullets(pr.bullets, tracks, p.headline, p.max_bullets_per_project,
                              boost, drop, f"project '{pr.id}'", warnings)
        projects.append({
            "id": pr.id,
            "name": f"{pr.name}: {subtitle}" if subtitle else pr.name,
            "dates": pr.display_date or fmt_range(pr.start, pr.end),
            "bullets": [bullet_dict(b, prio) for b, prio in chosen],
        })

    # ---- skills (grouped by category, YAML order preserved) -----------
    groups: dict[str, list[str]] = {}
    empty_receipts = []
    for sk in resume.skills:
        if not tracks & set(sk.tracks):
            continue
        if not sk.receipts:
            empty_receipts.append(sk.name)
            if p.hide_skills_without_receipts:
                continue
        groups.setdefault(sk.category, []).append(sk.name)
    skills = [{"category": c, "names": ", ".join(n)} for c, n in groups.items()]
    if empty_receipts and not p.hide_skills_without_receipts:
        warnings.append(f"skills shown with no receipts: {', '.join(empty_receipts)}")

    # ---- education ----------------------------------------------------
    education = [{
        "degree": e.degree,
        "school": e.school,
        "location": e.location or "",
        "gpa": f"{e.gpa:.1f}" if e.gpa else "",
        "coursework": ", ".join(c.name for c in e.coursework if tracks & set(c.tracks)),
    } for e in resume.education]

    # ---- certs --------------------------------------------------------
    certs = [
        c.name + (" (In Progress)" if c.status == "in_progress" else "")
        for c in resume.certs if tracks & set(c.tracks)
    ]

    selected_ids = [b["id"] for block in (*experience, *projects) for b in block["bullets"]]

    context = {
        "name": prof.name,
        "headline": chosen_headline,
        "headline_options": options,
        "contact": contact,
        "contact_line": " | ".join(contact),
        "languages": ", ".join(f"{l.name} ({l.level})" for l in prof.languages),
        "summary": resume.summary_by_id[p.summary].text,
        "sections": list(p.sections),
        "experience": experience,
        "projects": projects,
        "skills": skills,
        "education": education,
        "certs": certs,
        "certs_line": "  ·  ".join(certs),
        "meta": {"preset": preset_name, "company": company or ""},
    }
    return Selection(context=context, warnings=warnings, selected_bullet_ids=selected_ids)


# ---------------------------------------------------------------------
# Fit-to-page support (build.py calls this in a loop)
# ---------------------------------------------------------------------
def next_bullet_to_drop(sel: Selection) -> str | None:
    """
    Least valuable bullet: highest priority number, from the block with the
    most bullets, lowest block first (oldest role, least relevant project). Never empties a block, never drops a p1.
    """
    candidates = []
    blocks = [*sel.context["experience"], *sel.context["projects"]]
    n = len(blocks)
    for age, block in enumerate(blocks):          # blocks are newest first
        if len(block["bullets"]) <= 1:
            continue
        for pos, b in enumerate(block["bullets"]):
            if b["priority"] == 1:
                continue
            candidates.append((b["priority"], len(block["bullets"]), age / n, pos, b["id"]))
    if not candidates:
        return None
    return max(candidates)[-1]


# ---------------------------------------------------------------------
# Cover letter
# ---------------------------------------------------------------------
@dataclass
class CoverSelection:
    context: dict                          # hand straight to docxtpl (cover_template.docx)
    letter_id: str
    proof: list[dict] = field(default_factory=list)   # [{"refs", "priority", "text"}] as rendered
    why: str = ""
    warnings: list[str] = field(default_factory=list)


def pick_cover_letter(resume: Resume, preset_name: str) -> CoverLetter:
    """Letter for the preset's headline track first, then any of its tracks."""
    p = resume.presets[preset_name]
    for track in (p.headline, *p.tracks):
        for c in resume.cover_letters:
            if track in c.tracks:
                return c
    raise KeyError(
        f"no cover letter for preset '{preset_name}' (tracks: {', '.join(p.tracks)}). "
        f"Add one under cover_letters in master.yaml."
    )


def build_cover_context(
    resume: Resume,
    preset_name: str,
    resume_sel: Selection,
    company: str,
    role: str | None = None,
    hiring_manager: str = "",
    company_address: str = "",
    why: str = "",
    date: str = "",
    drop: set[int] | None = None,
) -> CoverSelection:
    """
    resume_sel: the Selection of the resume this letter ships with. A proof
                paragraph only goes in if at least one of its refs made it onto
                that resume, so the letter never brags about something the
                resume left out.
    why:        the "why this company" paragraph, written per job. Goes after
                the proof, right before the closer.
    drop:       proof indexes (YAML order) to leave out; the fit loop feeds this.
    """
    letter = pick_cover_letter(resume, preset_name)
    role = role or resume_sel.context["headline"]
    drop = drop or set()
    warnings: list[str] = []

    def fill(text: str) -> str:
        return text.format(company=company, role=role)

    on_resume = set(resume_sel.selected_bullet_ids)
    proof = []
    for i, pr in enumerate(letter.proof):
        if i in drop:
            continue
        if not on_resume & set(pr.refs):
            warnings.append(f"cover: skipped proof {i} ({', '.join(pr.refs)}): none of its bullets are on the resume")
            continue
        proof.append({"index": i, "refs": list(pr.refs), "priority": pr.priority, "text": fill(pr.text)})
    if not proof:
        warnings.append("cover: no proof paragraphs survived; letter is opener + closer only")
    if not why.strip():
        warnings.append("cover: no --why paragraph; this letter says nothing specific about the company")

    why = why.strip()
    body = [p["text"] for p in proof] + ([why] if why else [])
    ctx = resume_sel.context
    context = {
        "name": ctx["name"],
        "headline": ctx["headline"],
        "contact_line": ctx["contact_line"],
        "date": date,
        "hiring_manager": hiring_manager,
        "company": company,
        "company_address": company_address,
        "role": role,
        "opener": fill(letter.opener),
        "body": body,
        "closer": fill(letter.closer),
    }
    return CoverSelection(context=context, letter_id=letter.id, proof=proof, why=why,
                          warnings=warnings)


def next_proof_to_drop(sel: CoverSelection) -> int | None:
    """Lowest-value proof paragraph: highest priority number, latest in the letter. Never a p1."""
    candidates = [(p["priority"], p["index"]) for p in sel.proof if p["priority"] != 1]
    return max(candidates)[1] if candidates else None


# ---------------------------------------------------------------------
# Terminal preview
# ---------------------------------------------------------------------
def preview(sel: Selection) -> str:
    c = sel.context
    out = [c["name"].upper(), c["headline"], c["contact_line"], ""]
    for s in c["sections"]:
        out.append(s.upper())
        if s == "summary":
            out.append(c["summary"])
        elif s == "skills":
            out += [f"{g['category']}: {g['names']}" for g in c["skills"]]
        elif s in ("experience", "projects"):
            for blk in c[s]:
                head = f"{blk['title']} | {blk['company']}" if s == "experience" else blk["name"]
                out.append(f"{head}    {blk['dates']}")
                out += [f"  • {b['text']}" for b in blk["bullets"]]
        elif s == "education":
            for e in c["education"]:
                out.append(f"{e['degree']} | {e['school']}  GPA {e['gpa']}")
                if e["coursework"]:
                    out.append(f"  Coursework: {e['coursework']}")
        elif s == "certs":
            out.append(c["certs_line"])
        out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        sys.exit('usage: python selector.py <preset> [--boost tag1,tag2] [--headline "Firmware Engineer"]')
    boost = set()
    if "--boost" in args:
        boost = set(args[args.index("--boost") + 1].split(","))
    head = args[args.index("--headline") + 1] if "--headline" in args else None
    sel = build_context(load_resume(os.environ.get("RESUME_MASTER", "master.yaml")), args[0], boost_tags=boost, headline=head)
    print(preview(sel))
    if sel.warnings:
        print("⚠️  WARNINGS")
        for w in sel.warnings:
            print(f"  - {w}")
    print(f"\n{len(sel.selected_bullet_ids)} bullets selected. Next to drop if too long: "
          f"{next_bullet_to_drop(sel)}")