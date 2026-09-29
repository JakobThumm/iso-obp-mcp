"""SQLite storage and matching for the ISO term index.

One row per localized concept in `entries`, plus one row per normalized
designation variant in `designations`. Lookup is an exact match against
the variant table, which keeps "Not Defined" a statement about the index
rather than about string formatting. `entries_fts` backs the fuzzier
`search_terms` path.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .normalize import normalize, variants

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    label       TEXT NOT NULL,
    standard    TEXT,
    owner       TEXT,
    status      TEXT,
    origin      TEXT,
    kind        TEXT NOT NULL DEFAULT 'register',
    concepts    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS entries (
    id            INTEGER PRIMARY KEY,
    source_id     INTEGER NOT NULL REFERENCES sources(id),
    concept_id    TEXT,
    identifier    TEXT,
    designation   TEXT NOT NULL,
    synonyms      TEXT,
    definition    TEXT NOT NULL,
    notes         TEXT,
    examples      TEXT,
    language      TEXT NOT NULL DEFAULT 'eng',
    standard      TEXT,
    clause        TEXT,
    link          TEXT,
    status        TEXT,
    deprecated    INTEGER NOT NULL DEFAULT 0,
    authority     TEXT NOT NULL DEFAULT 'register'
);

CREATE TABLE IF NOT EXISTS designations (
    entry_id   INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    norm       TEXT NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0,
    deprecated INTEGER NOT NULL DEFAULT 0,
    language   TEXT NOT NULL DEFAULT 'eng'
);

CREATE INDEX IF NOT EXISTS idx_designations_norm ON designations(norm);
CREATE INDEX IF NOT EXISTS idx_entries_source ON entries(source_id);

CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
    designation, synonyms, definition,
    content='entries', content_rowid='id', tokenize='unicode61'
);
"""


# How much weight a definition carries. The first two come from published
# standards; the last two are added by the user and must never be mistaken
# for them, because the whole question this server answers is whether a term
# has a definition in a standard.
AUTHORITY_REGISTER = "register"          # open published terminology register
AUTHORITY_PREVIEW = "standard-preview"   # OBP preview of the standard itself
AUTHORITY_PDF = "standard-pdf"           # extracted from a licensed PDF
AUTHORITY_TRANSCRIBED = "user-transcribed"  # user copied it from a standard
AUTHORITY_USER = "user-defined"          # the user's own working definition

#: Authoritative definitions sort and report ahead of user-supplied ones.
AUTHORITY_RANK = {
    AUTHORITY_REGISTER: 0,
    AUTHORITY_PREVIEW: 0,
    AUTHORITY_PDF: 0,
    AUTHORITY_TRANSCRIBED: 1,
    AUTHORITY_USER: 2,
}

#: Authorities that cite a published standard and clause. A transcription
#: belongs here: it is a real standard definition, just typed in by hand.
STANDARD_AUTHORITIES = frozenset(
    {AUTHORITY_REGISTER, AUTHORITY_PREVIEW, AUTHORITY_PDF, AUTHORITY_TRANSCRIBED}
)

#: Authorities extracted from the published text by tooling rather than
#: entered by hand, so the wording can be trusted verbatim.
VERIFIED_AUTHORITIES = frozenset(
    {AUTHORITY_REGISTER, AUTHORITY_PREVIEW, AUTHORITY_PDF}
)

#: Authorities the user supplied themselves.
USER_AUTHORITIES = frozenset({AUTHORITY_TRANSCRIBED, AUTHORITY_USER})


@dataclass
class Entry:
    """One localized term record ready to be indexed."""

    designation: str
    definition: str
    concept_id: str | None = None
    identifier: str | None = None
    synonyms: list[str] = field(default_factory=list)
    deprecated_synonyms: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    language: str = "eng"
    standard: str | None = None
    clause: str | None = None
    link: str | None = None
    status: str | None = None
    deprecated: bool = False
    authority: str = AUTHORITY_REGISTER


def data_dir() -> Path | None:
    """The user's data directory, if they have set one.

    Set ISO_OBP_DATA to keep the index, the glossary and any extracted
    standard terms together in one place, for example a private repository.
    """
    env = os.environ.get("ISO_OBP_DATA")
    return Path(env).expanduser() if env else None


def default_db_path() -> Path:
    """Index location.

    Precedence, most specific first: ISO_OBP_DB names the file outright;
    ISO_OBP_DATA names a directory holding it; otherwise it falls back to the
    XDG data directory.
    """
    env = os.environ.get("ISO_OBP_DB")
    if env:
        return Path(env).expanduser()
    root = data_dir()
    if root:
        return root / "terms.db"
    base = os.environ.get("XDG_DATA_HOME", "~/.local/share")
    return Path(base).expanduser() / "iso-obp-mcp" / "terms.db"


def connect(
    path: Path | None = None,
    *,
    create: bool = True,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """Open the index, applying the schema and any pending migration.

    Set `check_same_thread=False` when the caller may reach the connection
    from more than one thread, as the MCP server does; the caller is then
    responsible for serializing access.
    """
    target = path or default_db_path()
    if create:
        target.parent.mkdir(parents=True, exist_ok=True)
    elif not target.exists():
        raise FileNotFoundError(target)
    conn = sqlite3.connect(target, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if create:
        conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an index built by an earlier version up to the current schema.

    CREATE TABLE IF NOT EXISTS leaves an existing table alone, so columns
    added later have to be applied explicitly. Re-ingesting a source
    repopulates the new columns; until then they hold their defaults.
    """
    try:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(designations)")
        }
    except sqlite3.DatabaseError:
        return
    if columns and "deprecated" not in columns:
        conn.execute(
            "ALTER TABLE designations ADD COLUMN deprecated INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()

    for table, column, spec in (
        ("sources", "kind", "TEXT NOT NULL DEFAULT 'register'"),
        ("entries", "authority", "TEXT NOT NULL DEFAULT 'register'"),
    ):
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if existing and column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")
            conn.commit()


def replace_source(
    conn: sqlite3.Connection,
    *,
    slug: str,
    label: str,
    entries: list[Entry],
    standard: str | None = None,
    owner: str | None = None,
    status: str | None = None,
    origin: str | None = None,
    kind: str = "register",
) -> int:
    """Insert or replace one corpus wholesale.

    Re-ingesting a dataset drops its previous rows first so the index stays
    idempotent across runs.
    """
    row = conn.execute("SELECT id FROM sources WHERE slug = ?", (slug,)).fetchone()
    if row:
        old_id = row["id"]
        conn.execute(
            "DELETE FROM entries_fts WHERE rowid IN "
            "(SELECT id FROM entries WHERE source_id = ?)",
            (old_id,),
        )
        conn.execute(
            "DELETE FROM designations WHERE entry_id IN "
            "(SELECT id FROM entries WHERE source_id = ?)",
            (old_id,),
        )
        conn.execute("DELETE FROM entries WHERE source_id = ?", (old_id,))
        conn.execute("DELETE FROM sources WHERE id = ?", (old_id,))

    cursor = conn.execute(
        "INSERT INTO sources (slug, label, standard, owner, status, origin, kind, "
        "concepts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (slug, label, standard, owner, status, origin, kind, len(entries)),
    )
    source_id = cursor.lastrowid

    for entry in entries:
        _insert_entry(conn, source_id, entry)

    conn.commit()
    return len(entries)


def _insert_entry(conn: sqlite3.Connection, source_id: int, entry: Entry) -> None:
    synonyms = " | ".join(entry.synonyms)
    cursor = conn.execute(
        "INSERT INTO entries (source_id, concept_id, identifier, designation, "
        "synonyms, definition, notes, examples, language, standard, clause, link, "
        "status, deprecated, authority) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            source_id,
            entry.concept_id,
            entry.identifier,
            entry.designation,
            synonyms,
            entry.definition,
            "\n".join(entry.notes),
            "\n".join(entry.examples),
            entry.language,
            entry.standard,
            entry.clause,
            entry.link,
            entry.status,
            int(entry.deprecated),
            entry.authority,
        ),
    )
    entry_id = cursor.lastrowid

    conn.execute(
        "INSERT INTO entries_fts (rowid, designation, synonyms, definition) "
        "VALUES (?,?,?,?)",
        (entry_id, entry.designation, synonyms, entry.definition),
    )

    # The preferred designation is primary; synonyms are findable but marked,
    # so a caller can tell an exact term from an admitted synonym. Deprecated
    # designations are marked separately, because a paper using one should be
    # told to switch to the preferred term.
    deprecated_norms = {
        norm for term in entry.deprecated_synonyms for norm in variants(term)
    }
    seen: set[str] = set()
    for index, term in enumerate([entry.designation, *entry.synonyms]):
        for norm in variants(term):
            if norm in seen:
                continue
            seen.add(norm)
            conn.execute(
                "INSERT INTO designations "
                "(entry_id, norm, is_primary, deprecated, language) "
                "VALUES (?,?,?,?,?)",
                (
                    entry_id,
                    norm,
                    1 if index == 0 else 0,
                    1 if norm in deprecated_norms else 0,
                    entry.language,
                ),
            )


_ENTRY_COLUMNS = """
    e.designation, e.synonyms, e.definition, e.notes, e.examples, e.identifier,
    e.language, e.standard, e.clause, e.link, e.status, e.deprecated, e.authority,
    s.label AS source_label, s.slug AS source_slug, s.standard AS source_standard,
    s.kind AS source_kind
"""

# Authoritative definitions must sort ahead of user-supplied ones. Expressed
# in SQL because ordering happens in the query, not in Python.
_AUTHORITY_ORDER_SQL = """
    CASE e.authority
        WHEN 'register' THEN 0
        WHEN 'standard-preview' THEN 0
        WHEN 'standard-pdf' THEN 0
        WHEN 'user-transcribed' THEN 1
        WHEN 'user-defined' THEN 2
        ELSE 3
    END
"""


def _row_to_match(row: sqlite3.Row, *, exact: bool) -> dict:
    authority = row["authority"] or AUTHORITY_REGISTER
    # A user's own definition cites no standard, so leave the field null
    # rather than falling back to the corpus label, which would read like the
    # name of a standard.
    standard = row["standard"] or (
        row["source_standard"] or row["source_label"]
        if authority != AUTHORITY_USER
        else None
    )
    match = {
        "term": row["designation"],
        "definition": row["definition"],
        "standard": standard,
        "clause": row["clause"],
        "corpus": row["source_label"],
        "language": row["language"],
        "match_type": "exact" if exact else "synonym",
        "authority": authority,
        # Spelled out so a caller cannot conflate the user's own convention
        # with a definition published in a standard.
        "from_standard": authority in STANDARD_AUTHORITIES,
        "verbatim_from_source": authority in VERIFIED_AUTHORITIES,
        "user_supplied": authority in USER_AUTHORITIES,
    }
    if row["identifier"]:
        match["entry_number"] = row["identifier"]
    if row["synonyms"]:
        match["synonyms"] = row["synonyms"].split(" | ")
    if row["notes"]:
        match["notes"] = row["notes"].split("\n")
    if row["examples"]:
        match["examples"] = row["examples"].split("\n")
    if row["link"]:
        match["link"] = row["link"]
    if row["status"] and row["status"] != "valid":
        match["status"] = row["status"]
    if row["deprecated"]:
        match["deprecated"] = True
    return match


def lookup(
    conn: sqlite3.Connection, term: str, *, language: str = "eng"
) -> list[dict]:
    """Exact-variant lookup. Returns every match, across all standards.

    Returning a single definition would be wrong: "safety" and "hazard" are
    defined differently in different standards, and the caller needs to see
    the disagreement.
    """
    norm = normalize(term)
    if not norm:
        return []

    rows = conn.execute(
        f"""
        SELECT {_ENTRY_COLUMNS}, MAX(d.is_primary) AS is_primary,
               MIN(d.deprecated) AS matched_deprecated
        FROM designations d
        JOIN entries e ON e.id = d.entry_id
        JOIN sources s ON s.id = e.source_id
        WHERE d.norm = ? AND e.language = ?
        GROUP BY e.id
        ORDER BY {_AUTHORITY_ORDER_SQL}, is_primary DESC, e.standard, e.identifier
        """,
        (norm, language),
    ).fetchall()

    matches = []
    for row in rows:
        match = _row_to_match(row, exact=bool(row["is_primary"]))
        if not row["is_primary"] and row["matched_deprecated"]:
            # The caller searched a term ISO has deprecated; say so, and the
            # 'term' field already carries the designation to use instead.
            match["match_type"] = "deprecated_synonym"
            match["use_instead"] = row["designation"]
        matches.append(match)
    return matches


def search(
    conn: sqlite3.Connection, query: str, *, language: str = "eng", limit: int = 10
) -> list[dict]:
    """Full-text search over designations and definitions.

    Used to suggest candidates when an exact lookup misses, so the caller can
    tell "no ISO definition" from "ISO words it differently".
    """
    norm = normalize(query)
    if not norm:
        return []
    # Quote each token so FTS5 operators in user input cannot break the query.
    tokens = [f'"{token}"' for token in norm.split()]

    sql = f"""
        SELECT {_ENTRY_COLUMNS}, bm25(entries_fts) AS score
        FROM entries_fts
        JOIN entries e ON e.id = entries_fts.rowid
        JOIN sources s ON s.id = e.source_id
        WHERE entries_fts MATCH ? AND e.language = ?
        ORDER BY {_AUTHORITY_ORDER_SQL}, score
        LIMIT ?
    """

    # Require all tokens first. A multi-word term that shares no single entry
    # still has useful near-neighbours, so fall back to any-token matching
    # rather than reporting nothing.
    rows = conn.execute(sql, (" AND ".join(tokens), language, limit)).fetchall()
    if not rows and len(tokens) > 1:
        rows = conn.execute(sql, (" OR ".join(tokens), language, limit)).fetchall()

    results = []
    for row in rows:
        match = _row_to_match(row, exact=False)
        match["match_type"] = "search"
        results.append(match)
    return results


def corpus_info(conn: sqlite3.Connection) -> dict:
    """Describe exactly what is indexed, so 'Not Defined' can be qualified."""
    sources = conn.execute(
        "SELECT slug, label, standard, owner, status, origin, kind, concepts "
        "FROM sources ORDER BY kind, label"
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()["n"]
    languages = [
        row["language"]
        for row in conn.execute(
            "SELECT language, COUNT(*) AS n FROM entries GROUP BY language "
            "ORDER BY n DESC"
        ).fetchall()
    ]
    by_authority = {
        row["authority"]: row["n"]
        for row in conn.execute(
            "SELECT authority, COUNT(*) AS n FROM entries GROUP BY authority"
        ).fetchall()
    }
    from_standards = sum(
        count
        for authority, count in by_authority.items()
        if authority in VERIFIED_AUTHORITIES
    )
    return {
        "total_entries": total,
        "entries_from_standards": from_standards,
        "entries_added_by_user": total - from_standards,
        "by_authority": by_authority,
        "languages": languages,
        "standards_indexed": sum(
            1 for row in sources if row["kind"] != "user"
        ),
        "sources": [dict(row) for row in sources],
    }


def user_entries(conn: sqlite3.Connection, *, slug: str) -> list[dict]:
    """List the terms the user added, for review and removal."""
    rows = conn.execute(
        f"""
        SELECT {_ENTRY_COLUMNS}
        FROM entries e
        JOIN sources s ON s.id = e.source_id
        WHERE s.slug = ?
        ORDER BY e.designation
        """,
        (slug,),
    ).fetchall()
    return [_row_to_match(row, exact=True) for row in rows]
