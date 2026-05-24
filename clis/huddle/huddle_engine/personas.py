"""Loads persona summaries from the huddle skill's persona files.

We don't duplicate persona content here. We point at the existing roster.
"""
from __future__ import annotations
import os
import re
from dataclasses import dataclass
from pathlib import Path

ENV_ROSTER = "HUDDLE_PERSONA_DIR"

# Vendored roster — huddle-enterprise/personas/ at the repo root. Lets the
# autonomous huddle run without any dependency on the sibling huddle skill.
DEFAULT_ROSTER = Path(__file__).resolve().parents[3] / "personas"


@dataclass
class PersonaSummary:
    key: str            # filename stem, e.g. "maya-strategist"
    name: str           # displayName, e.g. "Maya"
    title: str
    icon: str
    role: str
    domains: list[str]
    primary_lens: str
    communication_style: str
    principles: str

    def to_brief(self) -> str:
        """Short prompt-friendly representation."""
        return (
            f"- **{self.name}** ({self.title}) [{self.key}]\n"
            f"    role: {self.role}\n"
            f"    domains: {', '.join(self.domains)}\n"
            f"    lens: {self.primary_lens}\n"
            f"    voice: {self.communication_style}\n"
            f"    principles: {self.principles}\n"
        )


def roster_dir() -> Path:
    override = os.environ.get(ENV_ROSTER)
    if override:
        return Path(override).expanduser()
    return DEFAULT_ROSTER


def load_all() -> list[PersonaSummary]:
    rd = roster_dir()
    if not rd.exists():
        raise FileNotFoundError(
            f"Persona roster not found: {rd}. Set {ENV_ROSTER} to override."
        )
    out: list[PersonaSummary] = []
    for f in sorted(rd.glob("*.md")):
        try:
            out.append(_parse_persona_file(f))
        except Exception as e:
            # skip malformed files but don't blow up
            print(f"[personas] skipping {f.name}: {e}")
    return out


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_persona_file(path: Path) -> PersonaSummary:
    raw = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        raise ValueError("no YAML frontmatter")
    fm = m.group(1)
    fields = _parse_yaml_ish(fm)
    return PersonaSummary(
        key=path.stem,
        name=fields.get("displayName", path.stem),
        title=fields.get("title", ""),
        icon=fields.get("icon", "").strip('"'),
        role=fields.get("role", ""),
        domains=_parse_list(fields.get("domains", "")),
        primary_lens=fields.get("primaryLens", "").strip('"'),
        communication_style=fields.get("communicationStyle", "").strip('"'),
        principles=fields.get("principles", "").strip('"'),
    )


def _parse_yaml_ish(block: str) -> dict[str, str]:
    """Tiny YAML subset parser: top-level `key: value` lines only.
    Avoids a yaml dependency. The persona frontmatter has no nesting.
    """
    out: dict[str, str] = {}
    current_key: str | None = None
    for raw_line in block.splitlines():
        if not raw_line.strip():
            continue
        m = re.match(r"^([A-Za-z_]+):\s*(.*)$", raw_line)
        if m:
            current_key = m.group(1)
            value = m.group(2).strip()
            out[current_key] = value
        elif current_key and raw_line.startswith((" ", "\t")):
            out[current_key] = (out[current_key] + " " + raw_line.strip()).strip()
    return out


def _parse_list(s: str) -> list[str]:
    s = s.strip()
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1]
        return [t.strip().strip('"') for t in inner.split(",") if t.strip()]
    return [t.strip() for t in s.split(",") if t.strip()]
