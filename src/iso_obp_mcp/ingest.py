"""Build the ISO term index from openly published terminology corpora.

Two ingestion paths:

1. Glossarist YAML registers (geolexica/*). These are the authoritative,
   openly published concept databases for their ISO committees, so they
   need no scraping.

2. NISO STS XML produced by `obp-access` from an OBP page saved out of
   band. The ISO OBP itself sits behind bot protection, so bulk automated
   fetching is neither possible nor appropriate; this path exists so a
   user can add standards from pages they opened in their own browser.
"""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import yaml

from . import glossary, pdf
from .db import (
    AUTHORITY_PDF,
    AUTHORITY_PREVIEW,
    AUTHORITY_TRANSCRIBED,
    AUTHORITY_USER,
    Entry,
    connect,
    corpus_info,
    data_dir as db_data_dir,
    default_db_path,
    replace_source,
)
from .normalize import clean_definition

TBX = "urn:iso:std:iso:30042:ed-2"


# --------------------------------------------------------------------------
# Glossarist YAML
# --------------------------------------------------------------------------


def _as_text(value) -> str:
    """Glossarist fields are sometimes a string, sometimes a list of blocks."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("content", ""))
    if isinstance(value, list):
        parts = [_as_text(item) for item in value]
        return "\n".join(part for part in parts if part)
    return str(value)


def _first_source(localized: dict) -> tuple[str | None, str | None, str | None]:
    """Extract (standard, clause, link) from the authoritative source, if any."""
    for source in localized.get("sources") or []:
        if not isinstance(source, dict):
            continue
        if source.get("type") not in (None, "authoritative"):
            continue
        origin = source.get("origin") or {}
        ref = origin.get("ref") or {}
        standard = ref.get("source") if isinstance(ref, dict) else str(ref)
        locality = origin.get("locality") or {}
        clause = locality.get("reference_from") if isinstance(locality, dict) else None
        return standard, (str(clause) if clause else None), origin.get("link")
    return None, None, None


def _localized_entries(docs: list[dict], register: dict) -> list[Entry]:
    """Turn one concept file's YAML document stream into Entry rows.

    The first document is the managed concept (identifier, status); the rest
    are per-language localizations carrying the designation and definition.
    """
    if not docs:
        return []

    managed = docs[0].get("data", {}) if isinstance(docs[0], dict) else {}
    identifier = managed.get("identifier")
    concept_status = docs[0].get("status") if isinstance(docs[0], dict) else None

    entries: list[Entry] = []
    for doc in docs[1:]:
        if not isinstance(doc, dict):
            continue
        localized = doc.get("data") or {}
        if not isinstance(localized, dict):
            continue

        terms = [t for t in (localized.get("terms") or []) if isinstance(t, dict)]
        designations = [
            str(t.get("designation")).strip()
            for t in terms
            if t.get("designation")
        ]
        if not designations:
            continue

        definition = clean_definition(_as_text(localized.get("definition")))
        if not definition:
            continue

        preferred_index = next(
            (
                i
                for i, t in enumerate(terms)
                if t.get("normative_status") == "preferred"
            ),
            0,
        )
        # Guard against terms/designations misalignment when an entry omits
        # a designation partway through the list.
        if preferred_index >= len(designations):
            preferred_index = 0
        primary = designations[preferred_index]
        synonyms = [d for i, d in enumerate(designations) if i != preferred_index]

        deprecated_designations = [
            str(t.get("designation")).strip()
            for t in terms
            if t.get("normative_status") == "deprecated" and t.get("designation")
        ]
        standard, clause, link = _first_source(localized)

        entries.append(
            Entry(
                designation=primary,
                definition=definition,
                concept_id=doc.get("id"),
                identifier=str(identifier) if identifier is not None else None,
                synonyms=synonyms,
                deprecated_synonyms=[
                    d for d in deprecated_designations if d != primary
                ],
                notes=[
                    n for n in
                    (_as_text(localized.get("notes")) or "").split("\n") if n
                ],
                examples=[
                    e for e in
                    (_as_text(localized.get("examples")) or "").split("\n") if e
                ],
                language=str(localized.get("language_code") or "eng"),
                standard=standard or register.get("ref"),
                clause=clause or (
                    str(identifier) if identifier is not None else None
                ),
                link=link,
                status=localized.get("entry_status") or concept_status,
                deprecated=primary in deprecated_designations,
            )
        )
    return entries


def _iev_standard(identifier: str | None, register: dict) -> str | None:
    """Derive the part-level standard reference for an IEV entry.

    IEV identifiers are "<part>-<section>-<number>", e.g. "351-42-06" belongs
    to IEC 60050-351. The register only names the vocabulary as a whole, so
    the part is recovered from the identifier to keep results citable.
    """
    base = register.get("ref") or "IEC 60050"
    if not identifier:
        return base
    part = str(identifier).split("-", 1)[0].strip()
    return f"{base}-{part}" if part.isdigit() else base


def _ingest_split_layout(
    conn,
    root: Path,
    concepts_dir: Path,
    *,
    slug: str | None,
    languages: set[str] | None,
) -> list[tuple[str, int]]:
    """Index a Glossarist register that stores localizations in separate files.

    The IEV dataset keeps managed concepts in `concepts/concept/` and each
    localization in `concepts/localized_concept/<uuid>.yaml`, rather than as
    one multi-document file per concept. Only the localizations actually
    referenced by a requested language are read, because the full set runs to
    hundreds of thousands of files.
    """
    register_path = root / "register.yaml"
    register: dict = {}
    if register_path.exists():
        loaded = yaml.safe_load(register_path.read_text(encoding="utf-8"))
        register = loaded if isinstance(loaded, dict) else {}

    localized_dir = concepts_dir / "localized_concept"
    entries: list[Entry] = []

    for path in sorted((concepts_dir / "concept").glob("*.yaml")):
        try:
            managed_doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            print(f"  ! skipping {path.name}: {exc}", file=sys.stderr)
            continue
        if not isinstance(managed_doc, dict):
            continue

        managed = managed_doc.get("data") or {}
        refs = managed.get("localized_concepts") or {}
        if not isinstance(refs, dict):
            continue

        # Reassemble the multi-document shape the shared parser expects.
        docs: list[dict] = [managed_doc]
        for language, uuid in refs.items():
            if languages and str(language) not in languages:
                continue
            localized_path = localized_dir / f"{uuid}.yaml"
            if not localized_path.exists():
                continue
            try:
                localized_doc = yaml.safe_load(
                    localized_path.read_text(encoding="utf-8")
                )
            except yaml.YAMLError:
                continue
            if not isinstance(localized_doc, dict):
                continue
            # A file with a bare "data:" key parses to None, so normalize
            # before writing the language the reference came from.
            payload = localized_doc.get("data")
            if not isinstance(payload, dict):
                payload = {}
                localized_doc["data"] = payload
            payload.setdefault("language_code", str(language))
            docs.append(localized_doc)

        if len(docs) < 2:
            continue

        for entry in _localized_entries(docs, register):
            # These registers carry no per-entry source; the identifier maps
            # onto the vocabulary part.
            if not entry.standard:
                entry.standard = _iev_standard(entry.identifier, register)
            entries.append(entry)

    if not entries:
        return []

    label = register.get("ref") or register.get("name") or root.name
    count = replace_source(
        conn,
        slug=slug or str(register.get("id") or root.name),
        label=str(label),
        entries=entries,
        standard=register.get("ref"),
        owner=register.get("owner"),
        status=register.get("status"),
        origin=register.get("source_repo") or str(root),
    )
    return [(str(label), count)]


def _find_concept_dirs(root: Path) -> list[Path]:
    """Locate concept directories across the layouts geolexica repos use.

    isotc211-glossary keeps `concepts/` at the root; isotc204-glossary nests
    one register per edition under `datasets/<name>/concepts/`.
    """
    direct = root / "concepts"
    if direct.is_dir():
        return [direct]
    nested = sorted(p for p in root.glob("*/*/concepts") if p.is_dir())
    return nested or sorted(p for p in root.glob("*/concepts") if p.is_dir())


def ingest_glossarist(
    conn,
    root: Path,
    *,
    slug: str | None = None,
    languages: set[str] | None = None,
) -> list[tuple[str, int]]:
    """Index every register found under a cloned Glossarist repository."""
    root = root.expanduser().resolve()

    # The IEV dataset splits concepts from their localizations; everything
    # else keeps one multi-document file per concept.
    split = root / "concepts" / "localized_concept"
    if split.is_dir():
        return _ingest_split_layout(
            conn, root, root / "concepts", slug=slug, languages=languages
        )

    concept_dirs = _find_concept_dirs(root)
    if not concept_dirs:
        raise SystemExit(
            f"No concepts/ directory found under {root}. "
            "Expected a cloned Glossarist register (e.g. geolexica/isotc211-glossary)."
        )

    results: list[tuple[str, int]] = []
    for concept_dir in concept_dirs:
        register_path = concept_dir.parent / "register.yaml"
        register: dict = {}
        if register_path.exists():
            try:
                loaded = yaml.safe_load(register_path.read_text(encoding="utf-8"))
                register = loaded if isinstance(loaded, dict) else {}
            except yaml.YAMLError:
                register = {}

        entries: list[Entry] = []
        for path in sorted(concept_dir.glob("*.yaml")):
            try:
                docs = [
                    d for d in yaml.safe_load_all(path.read_text(encoding="utf-8"))
                    if d is not None
                ]
            except yaml.YAMLError as exc:
                print(f"  ! skipping {path.name}: {exc}", file=sys.stderr)
                continue
            entries.extend(_localized_entries(docs, register))

        if not entries:
            continue

        register_id = register.get("id") or concept_dir.parent.name
        dataset_slug = (
            f"{slug}:{register_id}" if slug and len(concept_dirs) > 1
            else (slug or register_id)
        )
        label = register.get("ref") or register.get("name") or str(register_id)

        count = replace_source(
            conn,
            slug=str(dataset_slug),
            label=str(label),
            entries=entries,
            standard=register.get("ref"),
            owner=register.get("owner"),
            status=register.get("status"),
            origin=register.get("source_repo") or str(root),
        )
        results.append((str(label), count))
    return results


# --------------------------------------------------------------------------
# NISO STS XML (from obp-access)
# --------------------------------------------------------------------------


def _sts_text(node) -> str:
    return clean_definition(" ".join(node.itertext())) if node is not None else ""


def ingest_sts(conn, path: Path, *, slug: str | None = None) -> tuple[str, int]:
    """Index term entries from a NISO STS XML file produced by obp-access."""
    path = path.expanduser().resolve()
    root = ET.parse(path).getroot()

    std_ref = root.find(".//{*}std-ref")
    label = _sts_text(std_ref) or path.stem

    # Preferred designations come first, then admitted, then deprecated, so
    # the primary term of an entry is the one a paper should actually use.
    auth_rank = {"preferredTerm": 0, "admittedTerm": 1, "deprecatedTerm": 2}

    entries: list[Entry] = []
    for term_entry in root.iter(f"{{{TBX}}}termEntry"):
        identifier = (term_entry.get("id") or "").replace("term_", "").strip() or None

        for lang_set in term_entry.findall(f"{{{TBX}}}langSet"):
            language = (
                lang_set.get("{http://www.w3.org/XML/1998/namespace}lang") or "en"
            )

            tigs = []
            for tig in lang_set.findall(f"{{{TBX}}}tig"):
                text = _sts_text(tig.find(f"{{{TBX}}}term"))
                if not text:
                    continue
                auth_node = tig.find(f"{{{TBX}}}normativeAuthorization")
                auth = (
                    auth_node.get("value") if auth_node is not None else "preferredTerm"
                ) or "preferredTerm"
                tigs.append((auth_rank.get(auth, 1), text, auth, tig.get("id") or ""))

            definition = _sts_text(lang_set.find(f".//{{{TBX}}}definition"))
            if not tigs or not definition:
                continue

            tigs.sort(key=lambda item: item[0])
            designations = [item[1] for item in tigs]

            # obp-access leaves termEntry/@id empty when the OBP markup puts
            # the clause label outside the entry; the tig id still carries it
            # as "term_<clause>-<index>".
            clause = identifier
            if not clause:
                for _, _, _, tig_id in tigs:
                    candidate = tig_id.replace("term_", "").rsplit("-", 1)[0].strip()
                    if candidate:
                        clause = candidate
                        break

            entries.append(
                Entry(
                    designation=designations[0],
                    definition=definition,
                    identifier=clause,
                    synonyms=designations[1:],
                    deprecated_synonyms=[
                        item[1] for item in tigs[1:] if item[2] == "deprecatedTerm"
                    ],
                    language="eng" if language.startswith("en") else language,
                    standard=label,
                    clause=clause,
                    deprecated=tigs[0][2] == "deprecatedTerm",
                    authority=AUTHORITY_PREVIEW,
                )
            )

    count = replace_source(
        conn,
        slug=slug or f"sts:{path.stem}",
        label=label,
        entries=entries,
        standard=label,
        origin=str(path),
    )
    return label, count


# --------------------------------------------------------------------------
# Licensed standard PDFs
# --------------------------------------------------------------------------


def _slugify(reference: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", reference.lower()).strip("-")


def standards_dir(root: Path | None = None) -> Path:
    """Where extracted term files live, inside the user's data directory."""
    base = root or db_data_dir()
    if base is None:
        raise SystemExit(
            "Set ISO_OBP_DATA to a directory for extracted standard terms, "
            "or pass --out explicitly."
        )
    return base / "standards"


def extract_pdf(
    path: Path,
    *,
    standard: str | None = None,
    language: str = "eng",
    out_dir: Path | None = None,
) -> tuple[Path, str, int]:
    """Extract a standard's terms clause to a reviewable YAML file.

    Extraction is separated from indexing so the result can be inspected and
    version-controlled before it starts answering lookups.
    """
    path = path.expanduser().resolve()
    reference = standard or pdf.document_reference(path) or path.stem

    terms = pdf.extract(path)
    # Refuse to write anything carrying the licensee watermark.
    pdf.assert_clean(terms)
    if not terms:
        raise SystemExit(
            f"No terms and definitions clause found in {path.name}. "
            "Pass --standard if the document reference is unusual, or check "
            "that the PDF has a text layer."
        )

    target_dir = out_dir or standards_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{_slugify(reference)}.yaml"

    payload = {
        "standard": reference,
        "language": language,
        # The file name only, never the full path: it may reveal a private tree.
        "source_pdf": path.name,
        "extracted": date.today().isoformat(),
        "terms": [term.to_mapping() for term in terms],
    }
    target.write_text(
        "# Extracted from a licensed PDF by iso-obp-index pdf. Review before\n"
        "# relying on it: the definitions are read from the PDF text layer.\n"
        + yaml.safe_dump(
            payload, sort_keys=False, allow_unicode=True, width=88
        ),
        encoding="utf-8",
    )
    return target, reference, len(terms)


def ingest_standard_file(conn, path: Path) -> tuple[str, int]:
    """Index one extracted standard file."""
    path = path.expanduser().resolve()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    reference = str(data.get("standard") or path.stem)
    language = str(data.get("language") or "eng")

    entries = []
    for raw in data.get("terms") or []:
        if not isinstance(raw, dict):
            continue
        designation = str(raw.get("term") or "").strip()
        definition = clean_definition(str(raw.get("definition") or ""))
        if not designation or not definition:
            continue
        entries.append(
            Entry(
                designation=designation,
                definition=definition,
                identifier=(str(raw["clause"]).strip() if raw.get("clause") else None),
                clause=(str(raw["clause"]).strip() if raw.get("clause") else None),
                synonyms=[str(x).strip() for x in (raw.get("synonyms") or [])],
                notes=[str(x).strip() for x in (raw.get("notes") or [])],
                examples=[str(x).strip() for x in (raw.get("examples") or [])],
                language=language,
                standard=reference,
                authority=AUTHORITY_PDF,
            )
        )

    replace_source(
        conn,
        slug=f"pdf:{_slugify(reference)}",
        label=reference,
        entries=entries,
        standard=reference,
        origin=str(data.get("source_pdf") or path.name),
        kind="standard",
    )
    return reference, len(entries)


def ingest_standards(conn, directory: Path | None = None) -> list[tuple[str, int]]:
    """Index every extracted standard file in the standards directory."""
    target = directory or standards_dir()
    if not target.is_dir():
        return []
    return [
        ingest_standard_file(conn, path)
        for path in sorted(target.glob("*.yaml"))
    ]


# --------------------------------------------------------------------------
# User glossary
# --------------------------------------------------------------------------


def ingest_user_glossary(conn, path: Path | None = None) -> tuple[Path, int]:
    """Index the user's own glossary, replacing whatever was there before.

    An entry naming a standard is recorded as transcribed from that standard;
    an entry without one is recorded as the user's own definition. The two
    are never reported as equivalent to a definition from an open register.
    """
    target = path or glossary.default_path()
    terms = glossary.load(target)

    entries = [
        Entry(
            designation=term.term,
            definition=term.definition,
            synonyms=term.synonyms or [],
            notes=term.notes or [],
            examples=term.examples or [],
            language=term.language,
            standard=term.standard,
            clause=term.clause,
            link=term.link,
            status=term.source_note,
            authority=(
                AUTHORITY_TRANSCRIBED if term.standard else AUTHORITY_USER
            ),
        )
        for term in terms
    ]

    replace_source(
        conn,
        slug=glossary.SLUG,
        label=glossary.LABEL,
        entries=entries,
        origin=str(target),
        kind="user",
    )
    return target, len(entries)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="iso-obp-index",
        description="Build the ISO term index used by the iso-obp-mcp server.",
    )
    parser.add_argument("--db", type=Path, default=None, help="index path")
    sub = parser.add_subparsers(dest="command", required=True)

    glossarist = sub.add_parser(
        "glossarist", help="index a cloned Glossarist register (geolexica/*)"
    )
    glossarist.add_argument("path", type=Path)
    glossarist.add_argument("--slug", default=None)
    glossarist.add_argument(
        "--languages",
        default=None,
        help=(
            "comma-separated ISO 639-2/T codes to index, e.g. 'eng'. "
            "Limits work on very large registers; default is all languages."
        ),
    )

    sts = sub.add_parser("sts", help="index NISO STS XML produced by obp-access")
    sts.add_argument("path", type=Path)
    sts.add_argument("--slug", default=None)

    pdf_cmd = sub.add_parser(
        "pdf", help="extract terms from a licensed standard PDF, then index"
    )
    pdf_cmd.add_argument("paths", type=Path, nargs="+", help="PDF file(s)")
    pdf_cmd.add_argument(
        "--standard", default=None, help="override the document reference"
    )
    pdf_cmd.add_argument("--language", default="eng", help="ISO 639-2/T code")
    pdf_cmd.add_argument(
        "--out", type=Path, default=None, help="directory for extracted YAML"
    )
    pdf_cmd.add_argument(
        "--extract-only",
        action="store_true",
        help="write the YAML but do not index it yet",
    )

    standards = sub.add_parser(
        "standards", help="index every extracted standard file"
    )
    standards.add_argument("dir", type=Path, nargs="?", default=None)

    user = sub.add_parser(
        "user", help="index the user's own glossary of terms"
    )
    user.add_argument(
        "path", type=Path, nargs="?", default=None, help="glossary file"
    )
    user.add_argument(
        "--init",
        action="store_true",
        help="create the glossary from a template if it does not exist",
    )

    sub.add_parser("info", help="show what is currently indexed")

    args = parser.parse_args(argv)
    db_path = args.db or default_db_path()

    if args.command == "info":
        with connect(db_path, create=False) as conn:
            info = corpus_info(conn)
        print(f"Index: {db_path}")
        print(f"{info['total_entries']} entries from {info['standards_indexed']} sources")
        for source in info["sources"]:
            status = f" [{source['status']}]" if source["status"] else ""
            mark = " (yours)" if source["kind"] == "user" else ""
            print(f"  {source['concepts']:>6}  {source['label']}{status}{mark}")
        if info["entries_added_by_user"]:
            print(
                f"\n{info['entries_from_standards']} from standards, "
                f"{info['entries_added_by_user']} added by you"
            )
        return 0

    with connect(db_path) as conn:
        if args.command == "glossarist":
            languages = (
                {code.strip() for code in args.languages.split(",") if code.strip()}
                if args.languages
                else None
            )
            for label, count in ingest_glossarist(
                conn, args.path, slug=args.slug, languages=languages
            ):
                print(f"Indexed {count} entries from {label}")
        elif args.command == "sts":
            label, count = ingest_sts(conn, args.path, slug=args.slug)
            print(f"Indexed {count} entries from {label}")
        elif args.command == "pdf":
            if args.standard and len(args.paths) > 1:
                raise SystemExit("--standard applies to a single PDF only")
            for path in args.paths:
                try:
                    target, reference, found = extract_pdf(
                        path,
                        standard=args.standard,
                        language=args.language,
                        out_dir=args.out,
                    )
                except SystemExit as exc:
                    print(f"  ! {path.name}: {exc}", file=sys.stderr)
                    continue
                print(f"Extracted {found} terms from {reference} -> {target}")
                if not args.extract_only:
                    _, count = ingest_standard_file(conn, target)
                    print(f"  indexed {count} entries")
        elif args.command == "standards":
            for reference, count in ingest_standards(conn, args.dir):
                print(f"Indexed {count} entries from {reference}")
        else:
            if args.init:
                created = glossary.ensure(args.path)
                print(f"Glossary: {created}")
            path, count = ingest_user_glossary(conn, args.path)
            print(f"Indexed {count} of your own terms from {path}")
    print(f"Index: {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
