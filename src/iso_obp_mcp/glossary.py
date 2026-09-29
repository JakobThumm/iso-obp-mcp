"""The user's own glossary: a YAML file they can read, edit and version.

Two kinds of entry live here, and the difference matters:

* A definition transcribed from a standard the user has access to. It names
  the standard, so it is a real standard definition that simply was not in
  any open register.
* The user's own working definition of a term, introduced deliberately as
  common knowledge for their writing. This is not a standard definition and
  the server never reports it as one.

The file is the source of truth. Every mutation writes the file first and
then rebuilds the index from it, because indexing a term without recording
it would lose it on the next re-ingest.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SLUG = "user"
LABEL = "User glossary"

TEMPLATE = """\
# Terms you add yourself, used by the iso-obp MCP server.
#
# Entries that name a `standard:` are treated as definitions from that
# standard, for standards you have access to but which are not published as
# open registers. Entries without one are your own working definitions, and
# are always reported as such rather than as standard definitions.
#
# Re-index after editing by hand:  iso-obp-index user
terms: []
"""


class GlossaryError(RuntimeError):
    """The glossary file exists but cannot be used as written."""


@dataclass
class Term:
    """One entry in the user's glossary."""

    term: str
    definition: str
    standard: str | None = None
    clause: str | None = None
    synonyms: list[str] | None = None
    notes: list[str] | None = None
    examples: list[str] | None = None
    language: str = "eng"
    link: str | None = None
    source_note: str | None = None

    def to_mapping(self) -> dict[str, Any]:
        """Render for YAML, omitting anything unset to keep the file readable."""
        data: dict[str, Any] = {"term": self.term, "definition": self.definition}
        for key, value in (
            ("standard", self.standard),
            ("clause", self.clause),
            ("synonyms", self.synonyms),
            ("notes", self.notes),
            ("examples", self.examples),
            ("link", self.link),
            ("source_note", self.source_note),
        ):
            if value:
                data[key] = value
        if self.language != "eng":
            data["language"] = self.language
        return data


def default_path() -> Path:
    """Glossary location.

    Precedence matches the index: ISO_OBP_GLOSSARY names the file outright,
    ISO_OBP_DATA names a directory holding it, otherwise the XDG config
    directory is used.
    """
    env = os.environ.get("ISO_OBP_GLOSSARY")
    if env:
        return Path(env).expanduser()
    from .db import data_dir

    root = data_dir()
    if root:
        return root / "glossary.yaml"
    base = os.environ.get("XDG_CONFIG_HOME", "~/.config")
    return Path(base).expanduser() / "iso-obp-mcp" / "glossary.yaml"


def ensure(path: Path | None = None) -> Path:
    """Create the glossary from the template if it does not exist yet."""
    target = path or default_path()
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(TEMPLATE, encoding="utf-8")
    return target


def _coerce_list(value: Any) -> list[str] | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
        return items or None
    return [str(value)]


def _coerce_term(raw: Any, *, index: int) -> Term:
    if not isinstance(raw, dict):
        raise GlossaryError(f"terms[{index}] is not a mapping")

    # "term" is the documented key; accept "designation" too, since that is
    # what the ISO registers call it.
    name = str(raw.get("term") or raw.get("designation") or "").strip()
    definition = str(raw.get("definition") or "").strip()
    if not name:
        raise GlossaryError(f"terms[{index}] has no 'term'")
    if not definition:
        raise GlossaryError(f"terms[{index}] ({name!r}) has no 'definition'")

    return Term(
        term=name,
        definition=definition,
        standard=(str(raw["standard"]).strip() if raw.get("standard") else None),
        clause=(str(raw["clause"]).strip() if raw.get("clause") else None),
        synonyms=_coerce_list(raw.get("synonyms")),
        notes=_coerce_list(raw.get("notes")),
        examples=_coerce_list(raw.get("examples")),
        language=str(raw.get("language") or "eng").strip() or "eng",
        link=(str(raw["link"]).strip() if raw.get("link") else None),
        source_note=(
            str(raw["source_note"]).strip() if raw.get("source_note") else None
        ),
    )


def load(path: Path | None = None) -> list[Term]:
    """Read the glossary. A missing file is an empty glossary, not an error."""
    target = path or default_path()
    if not target.exists():
        return []

    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise GlossaryError(f"{target} is not valid YAML: {exc}") from exc

    if data is None:
        return []
    # Accept a bare list as well as the documented {terms: [...]} mapping.
    raw_terms = data.get("terms") if isinstance(data, dict) else data
    if raw_terms is None:
        return []
    if not isinstance(raw_terms, list):
        raise GlossaryError(f"{target}: 'terms' must be a list")

    return [_coerce_term(raw, index=i) for i, raw in enumerate(raw_terms)]


def _dump(terms: list[Term]) -> str:
    body = yaml.safe_dump(
        {"terms": [term.to_mapping() for term in terms]},
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=88,
    )
    return TEMPLATE.replace("terms: []\n", "") + body


def save(terms: list[Term], path: Path | None = None) -> Path:
    """Rewrite the glossary from scratch.

    PyYAML cannot round-trip comments, so hand-written commentary below the
    header is lost here. `add` avoids that by appending instead; this path is
    only for removal, where the file has to be rebuilt.
    """
    target = path or default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_dump(terms), encoding="utf-8")
    return target


def _list_indent(text: str) -> str:
    """Indentation used by the existing `terms:` list items.

    A YAML sequence has to be indented consistently, so an appended block
    must line up with whatever the user's file already uses. Without this the
    append produces invalid YAML and the comment-preserving path is wasted.
    """
    in_terms = False
    for line in text.splitlines():
        stripped = line.strip()
        if not in_terms:
            if stripped.startswith("terms:"):
                in_terms = True
            continue
        if stripped.startswith("#") or not stripped:
            continue
        if stripped.startswith("- "):
            return " " * (len(line) - len(line.lstrip()))
        # Any other content means the list ended before an item appeared.
        break
    return ""


def _indent_block(block: str, indent: str) -> str:
    if not indent:
        return block
    return "".join(
        f"{indent}{line}" if line.strip() else line
        for line in block.splitlines(keepends=True)
    )


def add(term: Term, path: Path | None = None) -> Path:
    """Append one term, preserving whatever the user wrote above it.

    The append is text-level so comments and formatting survive. If the
    result does not parse back to a glossary containing the new term, the
    file is restored and rewritten from the parsed model instead.
    """
    target = ensure(path)
    original = target.read_text(encoding="utf-8")

    block = yaml.safe_dump(
        [term.to_mapping()],
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=88,
    )

    # An empty glossary carries a literal "terms: []", which cannot be
    # appended to; replace it with a real list header once.
    if "terms: []" in original:
        updated = original.replace("terms: []\n", "terms:\n").replace(
            "terms: []", "terms:\n"
        )
    else:
        updated = original if original.endswith("\n") else original + "\n"
    updated += _indent_block(block, _list_indent(updated))

    target.write_text(updated, encoding="utf-8")
    try:
        if any(existing.term == term.term for existing in load(target)):
            return target
    except GlossaryError:
        pass

    # The append did not produce a readable glossary, so fall back to a full
    # rewrite from the model rather than leaving the file broken.
    target.write_text(original, encoding="utf-8")
    existing_terms = load(target)
    existing_terms.append(term)
    return save(existing_terms, target)


def remove(name: str, path: Path | None = None) -> tuple[Path, list[str]]:
    """Drop entries matching `name`, returning the terms actually removed.

    A primary term match wins: only if nothing matches by primary term does
    a synonym match count, so removing "gripper" cannot quietly delete the
    "end effector" entry while another entry is named "gripper" outright.
    The caller is told which terms went, since a synonym match removes an
    entry the user did not name.
    """
    from .normalize import normalize

    target = path or default_path()
    wanted = normalize(name)
    terms = load(target)

    def by_primary(term: Term) -> bool:
        return normalize(term.term) == wanted

    def by_synonym(term: Term) -> bool:
        return wanted in {normalize(s) for s in (term.synonyms or [])}

    matches = by_primary if any(by_primary(t) for t in terms) else by_synonym
    removed = [term.term for term in terms if matches(term)]
    if removed:
        save([term for term in terms if not matches(term)], target)
    return target, removed
