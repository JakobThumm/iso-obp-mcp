"""MCP server exposing ISO term definitions.

Intended caller: a writing-assistance agent walking a paper and checking
whether each scientific term has a clear ISO definition.

The server is deliberately conservative about the negative answer. The
index covers whichever terminology corpora were ingested locally, not all
of ISO, so `not_defined` always reports what was actually searched. A
caller must be able to distinguish "ISO does not define this" from "this
index does not contain it".
"""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
from collections import Counter
from typing import Any

try:  # mcp >= 2.0 renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as _Server
except ModuleNotFoundError:  # pragma: no cover - mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from . import db, glossary
from .ingest import ingest_user_glossary

mcp = _Server(
    "iso-obp",
    instructions=(
        "Look up ISO definitions of scientific terms. Use lookup_term for a "
        "single term, lookup_terms to check many at once, and search_terms "
        "when an exact lookup misses. A 'not_defined' result means the term "
        "is absent from the corpora listed in the response, which is not the "
        "same as ISO defining it nowhere; call corpus_info to see the scope.\n\n"
        "Read the status field precisely. Only 'defined' means a standard in "
        "the index defines the term. 'defined_by_your_transcription' means the "
        "user typed it in from a standard they have, and "
        "'defined_by_you_only' means it is the user's own convention and needs "
        "introducing in the text.\n\n"
        "define_term and remove_term edit the user's glossary file on disk. "
        "Use them only for definitions the user supplied or explicitly "
        "confirmed, never to fill a gap you found, because that file is part "
        "of what later answers whether a term is defined."
    ),
)

_CONN: sqlite3.Connection | None = None

#: The SDK runs synchronous tool functions on a worker thread, so two calls
#: can reach the connection from different threads. The connection is opened
#: with check_same_thread=False and every use is serialized through this lock,
#: since a sqlite3 connection is not safe for concurrent use either.
_LOCK = threading.RLock()


def _conn() -> sqlite3.Connection:
    """Open the index lazily and keep it for the process lifetime."""
    global _CONN
    with _LOCK:
        if _CONN is None:
            path = db.default_db_path()
            if not path.exists():
                raise FileNotFoundError(
                    f"No ISO term index at {path}. Build one with: "
                    "iso-obp-index glossarist <path-to-cloned-register>"
                )
            _CONN = db.connect(path, check_same_thread=False)
        return _CONN


def _writable_conn() -> sqlite3.Connection:
    """Index connection for the glossary tools, creating the index if absent.

    Adding your own terms has to work before any register is indexed, so this
    does not require an existing index the way a lookup does.
    """
    global _CONN
    with _LOCK:
        if _CONN is None:
            _CONN = db.connect(
                db.default_db_path(), check_same_thread=False
            )
        return _CONN


def _scope(conn: sqlite3.Connection) -> dict[str, Any]:
    """Summarize the index so every answer can be read in context."""
    info = db.corpus_info(conn)
    scope = {
        "entries": info["total_entries"],
        "standards_indexed": info["standards_indexed"],
        "sources": [source["label"] for source in info["sources"]],
    }
    if info["entries_added_by_user"]:
        scope["entries_added_by_user"] = info["entries_added_by_user"]
    return scope


def _status_for(matches: list[dict]) -> str:
    """Classify a result set by the strongest authority it contains.

    A caller that only checks for "defined" must never be handed the user's
    own convention as though a standard defined it, so each weaker kind of
    definition gets its own status.
    """
    authorities = {match["authority"] for match in matches}
    if authorities & db.VERIFIED_AUTHORITIES:
        return "defined"
    if db.AUTHORITY_TRANSCRIBED in authorities:
        return "defined_by_your_transcription"
    if db.AUTHORITY_USER in authorities:
        return "defined_by_you_only"
    return "not_defined"


_STATUS_NOTE = {
    "defined_by_your_transcription": (
        "Defined only by an entry you transcribed from a standard you have "
        "access to. It cites the standard and clause, but the wording was "
        "entered by hand and is not verified against the published text."
    ),
    "defined_by_you_only": (
        "No standard in this index defines this term. The definition shown is "
        "your own working definition from your glossary, so it should be "
        "introduced explicitly in the text rather than treated as established "
        "terminology."
    ),
}


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _serialized(func):
    """Run a tool body under the connection lock.

    Applied beneath @mcp.tool() so the registered callable keeps the original
    signature and docstring, which is what the SDK turns into the tool schema.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _LOCK:
            return func(*args, **kwargs)

    return wrapper


@mcp.tool()
@_serialized
def lookup_term(term: str, language: str = "eng") -> str:
    """Look up the ISO definition of a scientific term.

    Returns every matching definition, because the same term is often
    defined differently by different standards and the disagreement matters
    when choosing terminology for a paper. When nothing matches exactly,
    the result is status "not_defined" together with near-miss suggestions
    and a description of which corpora were searched.

    Args:
        term: The term to look up, as written in the text.
        language: ISO 639-2/T language code of the definition (default "eng").

    Returns:
        JSON with status "defined" or "not_defined", the matches found, the
        suggestions when undefined, and the scope of the index searched.
    """
    try:
        conn = _conn()
    except FileNotFoundError as exc:
        return _dump({"status": "error", "error": str(exc)})

    query = (term or "").strip()
    if not query:
        return _dump({"status": "error", "error": "term must not be empty"})

    matches = db.lookup(conn, query, language=language)
    scope = _scope(conn)

    if matches:
        status = _status_for(matches)
        payload: dict[str, Any] = {
            "status": status,
            "query": query,
            "match_count": len(matches),
            "definitions": matches,
            "searched": scope,
        }
        if status in _STATUS_NOTE:
            payload["note"] = _STATUS_NOTE[status]
        return _dump(payload)

    suggestions = db.search(conn, query, language=language, limit=5)
    return _dump(
        {
            "status": "not_defined",
            "query": query,
            "definition": "Not Defined",
            "note": (
                "No exact ISO definition for this term in the indexed corpora. "
                "This means it is absent from the standards listed under "
                "'searched', not that ISO defines it nowhere."
            ),
            "suggestions": [
                {
                    "term": item["term"],
                    "standard": item["standard"],
                    "definition": item["definition"],
                }
                for item in suggestions
            ],
            "searched": scope,
        }
    )


@mcp.tool()
@_serialized
def lookup_terms(terms: list[str], language: str = "eng") -> str:
    """Look up many terms in one call.

    Checking a whole paper means dozens of lookups; batching keeps that to a
    single round trip. Each result carries the same status field as
    lookup_term, and the index scope is reported once for the batch.

    Args:
        terms: The terms to look up.
        language: ISO 639-2/T language code of the definition (default "eng").

    Returns:
        JSON with one result per term, plus counts of defined and undefined
        terms and the scope of the index searched.
    """
    try:
        conn = _conn()
    except FileNotFoundError as exc:
        return _dump({"status": "error", "error": str(exc)})

    results = []
    for raw in terms or []:
        query = (raw or "").strip()
        if not query:
            continue
        matches = db.lookup(conn, query, language=language)
        if matches:
            results.append(
                {
                    "term": query,
                    "status": _status_for(matches),
                    "match_count": len(matches),
                    "definitions": matches,
                }
            )
        else:
            results.append(
                {
                    "term": query,
                    "status": "not_defined",
                    "definition": "Not Defined",
                }
            )

    counts = Counter(item["status"] for item in results)
    return _dump(
        {
            "checked": len(results),
            "defined": counts["defined"],
            "defined_by_your_transcription": counts["defined_by_your_transcription"],
            "defined_by_you_only": counts["defined_by_you_only"],
            "not_defined": counts["not_defined"],
            "results": results,
            "searched": _scope(conn),
        }
    )


@mcp.tool()
@_serialized
def search_terms(query: str, language: str = "eng", limit: int = 10) -> str:
    """Search ISO terms and definitions by keyword.

    Use this when an exact lookup returns "Not Defined" and you want to know
    whether ISO expresses the same concept under a different designation,
    for example when a paper writes "neural net" and the standard says
    "artificial neural network".

    Args:
        query: Keywords to search for.
        language: ISO 639-2/T language code of the definition (default "eng").
        limit: Maximum number of results (default 10).

    Returns:
        JSON with the ranked matches and the scope of the index searched.
    """
    try:
        conn = _conn()
    except FileNotFoundError as exc:
        return _dump({"status": "error", "error": str(exc)})

    limit = max(1, min(int(limit or 10), 50))
    results = db.search(conn, query or "", language=language, limit=limit)
    return _dump(
        {
            "query": query,
            "result_count": len(results),
            "results": results,
            "searched": _scope(conn),
        }
    )


@mcp.tool()
@_serialized
def corpus_info() -> str:
    """Report exactly which terminology corpora this index covers.

    Call this before drawing conclusions from "Not Defined" results, so that
    the absence of a term can be attributed correctly.

    Returns:
        JSON listing every indexed source with its owner, status and entry
        count, plus total entries and available languages.
    """
    try:
        conn = _conn()
    except FileNotFoundError as exc:
        return _dump({"status": "error", "error": str(exc)})
    return _dump(db.corpus_info(conn))


@mcp.tool()
@_serialized
def define_term(
    term: str,
    definition: str,
    standard: str | None = None,
    clause: str | None = None,
    synonyms: list[str] | None = None,
    notes: list[str] | None = None,
    language: str = "eng",
    source_note: str | None = None,
) -> str:
    """Add a term to the user's own glossary and index it immediately.

    Use this for two cases. If the definition comes from a standard the user
    has access to but which is not in the index, pass `standard` (and
    `clause` if known) so the entry cites its source. If it is the user's own
    working definition of a term, leave `standard` unset: the entry is then
    reported as their own convention and never as a standard definition.

    Only add a definition the user has actually asked for or confirmed. This
    writes to a file that later answers "is this term defined", so inventing
    entries corrupts the thing the server is for.

    Args:
        term: The preferred designation.
        definition: The definition text.
        standard: Standard this is transcribed from, e.g. "ISO 8373:2021".
        clause: Clause number within that standard, e.g. "3.1".
        synonyms: Other designations that should find this entry.
        notes: Additional notes to record with the entry.
        language: ISO 639-2/T language code (default "eng").
        source_note: Free-text note on where the definition came from.

    Returns:
        JSON naming the glossary file written, the entry as recorded, and how
        it will be reported by lookups.
    """
    name = (term or "").strip()
    text = (definition or "").strip()
    if not name:
        return _dump({"status": "error", "error": "term must not be empty"})
    if not text:
        return _dump({"status": "error", "error": "definition must not be empty"})

    entry = glossary.Term(
        term=name,
        definition=text,
        standard=(standard or "").strip() or None,
        clause=(clause or "").strip() or None,
        synonyms=[s.strip() for s in (synonyms or []) if s and s.strip()] or None,
        notes=[n.strip() for n in (notes or []) if n and n.strip()] or None,
        language=(language or "eng").strip() or "eng",
        source_note=(source_note or "").strip() or None,
    )

    try:
        path = glossary.add(entry)
        conn = _writable_conn()
        _, count = ingest_user_glossary(conn, path)
    except (glossary.GlossaryError, OSError) as exc:
        return _dump({"status": "error", "error": str(exc)})

    authority = (
        db.AUTHORITY_TRANSCRIBED if entry.standard else db.AUTHORITY_USER
    )
    return _dump(
        {
            "status": "added",
            "glossary_file": str(path),
            "entry": entry.to_mapping(),
            "authority": authority,
            "reported_as": (
                f"transcribed from {entry.standard}"
                if entry.standard
                else "your own definition, not a standard definition"
            ),
            "terms_in_your_glossary": count,
        }
    )


@mcp.tool()
@_serialized
def remove_term(term: str) -> str:
    """Remove a term from the user's own glossary and reindex.

    Only affects the user's glossary; definitions from standards cannot be
    removed this way. Rewriting the file drops any comments the user wrote
    below its header.

    Matching prefers the primary term. If nothing matches there, a synonym
    match removes the entry carrying it, and the response names what went, so
    report the removed terms back to the user rather than only the count.

    Args:
        term: The term to remove, matched against designations then synonyms.

    Returns:
        JSON naming the terms removed and the file that was changed.
    """
    name = (term or "").strip()
    if not name:
        return _dump({"status": "error", "error": "term must not be empty"})

    try:
        path, removed = glossary.remove(name)
        conn = _writable_conn()
        _, count = ingest_user_glossary(conn, path)
    except (glossary.GlossaryError, OSError) as exc:
        return _dump({"status": "error", "error": str(exc)})

    payload = {
        "status": "removed" if removed else "not_found",
        "query": name,
        "removed_terms": removed,
        "removed": len(removed),
        "glossary_file": str(path),
        "terms_in_your_glossary": count,
    }
    if removed and not any(term.lower() == name.lower() for term in removed):
        payload["note"] = (
            f"{name!r} was a synonym, so the entry it belongs to was removed."
        )
    return _dump(payload)


@mcp.tool()
@_serialized
def list_my_terms() -> str:
    """List the terms the user has added to their own glossary.

    Returns:
        JSON with every entry, each marked as transcribed from a standard or
        as the user's own definition, plus the glossary file path.
    """
    try:
        conn = _writable_conn()
    except OSError as exc:
        return _dump({"status": "error", "error": str(exc)})

    entries = db.user_entries(conn, slug=glossary.SLUG)
    return _dump(
        {
            "glossary_file": str(glossary.default_path()),
            "count": len(entries),
            "transcribed_from_standards": sum(
                1 for e in entries if e["authority"] == db.AUTHORITY_TRANSCRIBED
            ),
            "your_own_definitions": sum(
                1 for e in entries if e["authority"] == db.AUTHORITY_USER
            ),
            "terms": entries,
        }
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
