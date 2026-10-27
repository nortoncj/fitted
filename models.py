"""
models.py : Pydantic v2 schema for master.yaml

Validation here = BROKEN data (typos, dangling ids, bad dates). Fails loud.
Smelly data (empty receipts, present tense on ended roles) goes in lint.py.

Usage:
    python models.py master.yaml     # smoke test, prints counts or errors
"""

from __future__ import annotations

import sys
from pathlib import Path
from string import Formatter
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

# ---------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------
Priority = Literal[1, 2, 3]
CertStatus = Literal["earned", "in_progress"]
Section = Literal["summary", "skills", "experience", "projects", "education", "certs"]

# "2025-09". Zero padded, so plain string comparison sorts correctly.
YearMonth = Annotated[str, StringConstraints(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]*$")]
NonEmpty = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]

# Tracks are data-driven: the keys of `headlines` in master.yaml ARE the track
# registry. Add a track in YAML, no code change. Typos like "sofware" get
# caught by Resume._integrity instead of silently matching nothing.
Track = Slug


class Strict(BaseModel):
    """Base: unknown keys are errors, so 'priorty: 1' can't silently vanish."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------
class Links(Strict):
    website: str | None = None
    linkedin: str | None = None
    github: str | None = None


class Language(Strict):
    name: NonEmpty
    level: NonEmpty


class Profile(Strict):
    name: NonEmpty
    phone: NonEmpty
    email: NonEmpty
    location: NonEmpty
    links: Links = Links()
    languages: list[Language] = []


# ---------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------
class Summary(Strict):
    id: Slug
    tracks: list[Track] = Field(min_length=1)
    text: NonEmpty


class Bullet(Strict):
    id: Slug
    tracks: list[Track] = Field(min_length=1)
    tags: list[str] = []
    # Either one number for every track, or per track:
    #   priority: {embedded: 1, devops: 2}
    # Tracks missing from the map default to 2.
    priority: Priority | dict[Track, Priority] = 2
    text: NonEmpty

    @model_validator(mode="after")
    def _priority_tracks_match(self):
        if isinstance(self.priority, dict):
            extra = set(self.priority) - set(self.tracks)
            if extra:
                raise ValueError(
                    f"bullet '{self.id}': priority set for {sorted(extra)} "
                    f"but bullet isn't tagged with those tracks"
                )
        return self

    def priority_for(self, primary: str, tracks: set[str]) -> int:
        """
        Priority as seen by a preset.
        - Bullet tagged with the preset's headline track: use that track's priority.
        - Bullet only matches a BORROWED track (e.g. an embedded bullet on the
          software preset): best borrowed priority, demoted one step. Borrowed
          bullets fill space; they never outrank on-message ones.
        """
        if primary in self.tracks:
            if isinstance(self.priority, int):
                return self.priority
            return self.priority.get(primary, 2)
        if isinstance(self.priority, int):
            best = self.priority
        else:
            relevant = [self.priority.get(t, 2) for t in self.tracks if t in tracks]
            best = min(relevant) if relevant else 2
        return min(3, best + 1)


class Dated(Strict):
    start: YearMonth
    end: YearMonth | None = None  # None = ongoing / present

    @model_validator(mode="after")
    def _end_after_start(self):
        if self.end is not None and self.end < self.start:
            raise ValueError(f"end {self.end} is before start {self.start}")
        return self

    @property
    def ongoing(self) -> bool:
        return self.end is None


class Role(Dated):
    id: Slug
    company: NonEmpty
    title: NonEmpty                      # official title, the one HR will confirm
    display_note: str | None = None      # renders as "Title (note)" if preset allows
    bullets: list[Bullet] = Field(min_length=1)


class Project(Dated):
    id: Slug
    name: NonEmpty
    subtitle: dict[Track, str] = {}      # per-track subtitle, optional
    display_date: str | None = None      # override, e.g. "2025"
    bullets: list[Bullet] = Field(min_length=1)


class Skill(Strict):
    name: NonEmpty
    category: NonEmpty
    tracks: list[Track] = Field(min_length=1)
    receipts: list[Slug] = []            # bullet or project ids; [] is a lint warning, not an error


class Course(Strict):
    name: NonEmpty
    tracks: list[Track] = Field(min_length=1)


class Education(Strict):
    id: Slug
    school: NonEmpty
    location: str | None = None
    degree: NonEmpty
    gpa: float | None = Field(default=None, ge=0, le=4.0)
    coursework: list[Course] = []


class Cert(Strict):
    name: NonEmpty
    status: CertStatus
    date: YearMonth | None = None
    tracks: list[Track] = Field(min_length=1)

    @model_validator(mode="after")
    def _earned_needs_date(self):
        # Soft rule: earned certs without a date are allowed (legacy entries),
        # but in_progress certs must NOT carry an earned date.
        if self.status == "in_progress" and self.date is not None:
            raise ValueError(f"{self.name}: in_progress cert has a date; did you pass it?")
        return self


# ---------------------------------------------------------------------
# Cover letters
# ---------------------------------------------------------------------
COVER_PLACEHOLDERS = {"company", "role"}


class CoverProof(Strict):
    refs: list[Slug] = Field(min_length=1)   # bullet ids that back this paragraph
    priority: Priority = 2
    text: NonEmpty


class CoverLetter(Strict):
    id: Slug
    tracks: list[Track] = Field(min_length=1)
    opener: NonEmpty
    proof: list[CoverProof] = Field(min_length=1)
    closer: NonEmpty

    @model_validator(mode="after")
    def _known_placeholders(self):
        # "{compnay}" should crash here, not render literally in a letter
        texts = [self.opener, self.closer, *(p.text for p in self.proof)]
        for t in texts:
            for _, field_name, _, _ in Formatter().parse(t):
                if field_name is not None and field_name not in COVER_PLACEHOLDERS:
                    raise ValueError(
                        f"cover letter '{self.id}': unknown placeholder '{{{field_name}}}' "
                        f"(allowed: {', '.join(sorted(COVER_PLACEHOLDERS))})"
                    )
        return self


# ---------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------
class Preset(Strict):
    headline: Track                      # key into Resume.headlines
    headline_variant: str | None = None  # must be in headlines[headline]; None = first in list
    summary: Slug                        # Summary.id
    tracks: list[Track] = Field(min_length=1)
    sections: list[Section] = Field(min_length=1)
    roles: list[Slug] = []
    projects: list[Slug] = []
    show_display_note: bool = False
    max_bullets_per_role: int = Field(default=3, ge=1, le=8)
    max_bullets_per_project: int = Field(default=3, ge=1, le=8)
    hide_skills_without_receipts: bool = False
    max_pages: int = Field(default=1, ge=1, le=2)

    @model_validator(mode="after")
    def _no_duplicate_sections(self):
        if len(set(self.sections)) != len(self.sections):
            raise ValueError("duplicate entries in sections")
        return self


# ---------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------
class Resume(Strict):
    profile: Profile
    # first entry in each list is the default headline for that track
    headlines: dict[Track, Annotated[list[NonEmpty], Field(min_length=1)]]
    summaries: list[Summary]
    cover_letters: list[CoverLetter] = []
    roles: list[Role]
    projects: list[Project] = []
    skills: list[Skill] = []
    education: list[Education] = []
    certs: list[Cert] = []
    presets: dict[NonEmpty, Preset]

    # ---- lookups -----------------------------------------------------
    @property
    def bullets(self) -> dict[str, Bullet]:
        return {b.id: b for item in [*self.roles, *self.projects] for b in item.bullets}

    @property
    def summary_by_id(self) -> dict[str, Summary]:
        return {s.id: s for s in self.summaries}

    @property
    def role_by_id(self) -> dict[str, Role]:
        return {r.id: r for r in self.roles}

    @property
    def project_by_id(self) -> dict[str, Project]:
        return {p.id: p for p in self.projects}

    # ---- cross-reference integrity ------------------------------------
    @model_validator(mode="after")
    def _integrity(self):
        errors: list[str] = []

        # 1. Every id is globally unique (receipts can point at bullets OR projects)
        all_ids = (
            [s.id for s in self.summaries]
            + [c.id for c in self.cover_letters]
            + [r.id for r in self.roles]
            + [p.id for p in self.projects]
            + [e.id for e in self.education]
            + [b.id for item in [*self.roles, *self.projects] for b in item.bullets]
        )
        seen: set[str] = set()
        for i in all_ids:
            if i in seen:
                errors.append(f"duplicate id: {i}")
            seen.add(i)

        # 2. Receipts point at something real
        receipt_targets = set(self.bullets) | set(self.project_by_id)
        for sk in self.skills:
            for r in sk.receipts:
                if r not in receipt_targets:
                    errors.append(f"skill '{sk.name}': receipt '{r}' doesn't exist")

        # 2b. Cover letter proof points at real bullets
        for c in self.cover_letters:
            for pr in c.proof:
                for r in pr.refs:
                    if r not in self.bullets:
                        errors.append(f"cover letter '{c.id}': ref '{r}' isn't a bullet id")

        # 3. Skill names unique (case-insensitive)
        names = [s.name.lower() for s in self.skills]
        for n in {n for n in names if names.count(n) > 1}:
            errors.append(f"duplicate skill: {n}")

        # 4. Every track used anywhere exists in the registry (headlines keys)
        known = set(self.headlines)
        usages: list[tuple[str, list[str]]] = (
            [(f"summary '{s.id}'", s.tracks) for s in self.summaries]
            + [(f"bullet '{b.id}'", b.tracks) for b in self.bullets.values()]
            + [(f"skill '{s.name}'", s.tracks) for s in self.skills]
            + [(f"cover letter '{c.id}'", c.tracks) for c in self.cover_letters]
            + [(f"cert '{c.name}'", c.tracks) for c in self.certs]
            + [(f"course '{c.name}'", c.tracks) for e in self.education for c in e.coursework]
            + [(f"project '{p.id}' subtitle", list(p.subtitle)) for p in self.projects]
            + [(f"preset '{n}'", p.tracks) for n, p in self.presets.items()]
        )
        for owner, tracks in usages:
            for t in tracks:
                if t not in known:
                    errors.append(f"{owner}: unknown track '{t}' (known: {', '.join(sorted(known))})")

        # 5. No duplicate headlines inside one track
        for t, titles in self.headlines.items():
            if len(set(titles)) != len(titles):
                errors.append(f"headlines '{t}': duplicate entries")

        # 6. Presets reference real things
        for pname, p in self.presets.items():
            if p.headline not in self.headlines:
                errors.append(f"preset '{pname}': headline '{p.headline}' not in headlines")
            elif p.headline_variant and p.headline_variant not in self.headlines[p.headline]:
                errors.append(
                    f"preset '{pname}': headline_variant '{p.headline_variant}' not in "
                    f"headlines['{p.headline}']"
                )
            if p.summary not in self.summary_by_id:
                errors.append(f"preset '{pname}': summary '{p.summary}' doesn't exist")
            for r in p.roles:
                if r not in self.role_by_id:
                    errors.append(f"preset '{pname}': role '{r}' doesn't exist")
            for pr in p.projects:
                if pr not in self.project_by_id:
                    errors.append(f"preset '{pname}': project '{pr}' doesn't exist")
            if p.projects and "projects" not in p.sections:
                errors.append(f"preset '{pname}': lists projects but 'projects' isn't in sections")

        if errors:
            raise ValueError("master.yaml integrity check failed:\n  " + "\n  ".join(errors))
        return self


# ---------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------
def load_resume(path: str | Path = "master.yaml") -> Resume:
    with open(path, encoding="utf-8") as f:
        return Resume.model_validate(yaml.safe_load(f))


def save_resume(resume: Resume, path: str | Path = "master.yaml") -> None:
    """
    WARNING: PyYAML drops comments. Resolve every CONFLICT / VERIFY comment
    before the UI starts writing to this file, or switch to ruamel.yaml.
    """
    data = resume.model_dump(mode="json", exclude_defaults=False)
    tmp = Path(path).with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, width=120)
    tmp.replace(path)  # atomic swap, a crash mid-write can't corrupt master.yaml


# ---------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------
if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "master.yaml"
    try:
        r = load_resume(path)
    except Exception as e:  # noqa: BLE001
        print(f"❌ {e}")
        sys.exit(1)
    print(
        f"✅ {path} valid: {len(r.roles)} roles, {len(r.projects)} projects, "
        f"{len(r.bullets)} bullets, {len(r.skills)} skills, presets: {', '.join(r.presets)}"
    )