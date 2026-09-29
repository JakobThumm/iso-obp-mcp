"""Extract terms and definitions from a DIN/ISO standard PDF.

For standards the user holds a licensed copy of but which are not published
as an open terminology register. The text layer of these PDFs carries the
structure needed to do this reliably:

* a clause heading is bold and has a title on the same line ("3  Terms and
  definitions");
* a term entry starts with a bold line that is *only* a number ("3.1");
* the designations that follow are bold, the definition is not;
* notes and examples are introduced by a recognizable label.

Only clauses whose heading names terminology are read. That restriction is
load-bearing: ISO 12100 has bold number-only lines in its risk-assessment
clauses that are list markers rather than term entries.

These PDFs are watermarked with the licensee's identity. `_WATERMARK` drops
those lines, and the extractor asserts nothing identifying survives.
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
from collections import Counter
from functools import lru_cache
from dataclasses import dataclass, field
from pathlib import Path

# Lines injected by the distributor's print watermark. These name the
# licensing institution and a per-user token, so they must never reach an
# extracted file.
_WATERMARK = re.compile(
    r"""
    Firmenname\s*:
  | Benutzername\s*:
  | _ip_user_
  | Datum\s*/\s*Uhrzeit\s+des\s+Ausdrucks
  | Printed\s+copies\s+are\s+uncontrolled
  | ^\s*©\s
  | All\s+rights\s+reserved
  | Alle\s+Rechte\s+vorbehalten
  | ^\s*Normenausschuss
  | ^\s*Gesamtumfang
  | Hochschul-Konsortium
  | Bibliothek\s*$
  | ^\s*\d{1,2}/\d{1,2}/\d{4}[\s,]+\d{1,2}:\d{2}
  """,
    re.IGNORECASE | re.VERBOSE,
)

#: Tokens that must never appear in extracted output. Asserted, not assumed.
FORBIDDEN = ("_ip_user_", "Firmenname", "Benutzername", "Uhrzeit des Ausdrucks")

# A running header repeats the document reference on every page, e.g.
# "DIN EN ISO 12100:2011-03" or "ISO 8373:2021(E)".
_RUNNING_HEADER = re.compile(
    r"^(?:DIN\s+)?(?:EN\s+)?(?:ISO|IEC)(?:/\w+)?\s+[\d\-]+(?:-\d+)?\s*:\s*[\d\-]+"
    r"(?:\s*\([A-Z]\))?$|^(?:DIN\s+)?(?:EN\s+)?(?:ISO|IEC)(?:/\w+)?\s+[\d\-]+"
    r":\d{4}(?:\([A-Z]\))?$",
    re.IGNORECASE,
)

# "3  Terms and definitions", "4  Terms related to mechanical structure",
# "3  Begriffe". The number is captured so entries can be tied to a clause.
_TERMS_HEADING = re.compile(
    r"^(\d+(?:\.\d+)*)\s+(?:terms?\b|begriffe\b|definitionen\b|"
    r"terminology\b|termini\b)",
    re.IGNORECASE,
)
# Any bold numbered heading with a title, which ends a terms clause.
_CLAUSE_HEADING = re.compile(r"^(\d+(?:\.\d+)*)\s{1,}(\D.*)$")
# A term entry number stands alone on its line.
_ENTRY_NUMBER = re.compile(r"^\d+(?:\.\d+)+$")

_NOTE = re.compile(
    r"^(?:NOTE|Note|ANMERKUNG|Anmerkung)\s*\d*\s*(?:to\s+entry)?\s*:?\s*(.*)$"
)
_EXAMPLE = re.compile(r"^(?:EXAMPLE|Example|BEISPIEL|Beispiel)S?\s*\d*\s*:?\s*(.*)$")
# A cross-reference number ISO appends to a referenced term: "autonomy (3.2)".
_XREF = re.compile(r"\s*\(\s*\d+(?:\.\d+)+\s*\)")
_WS = re.compile(r"\s+")
_BARE_INT = re.compile(r"^\d{1,4}$")
#: A table-of-contents line, recognizable by its dot leader. Without this the
#: contents listing is read as a run of clause headings.
_TOC_LEADER = re.compile(r"\.{5,}")
#: Fragments this far apart vertically still belong to one visual line. Draft
#: PDFs place an italic cross-reference a pixel off its surrounding text.
_LINE_TOLERANCE = 2


@dataclass
class ExtractedTerm:
    """One term entry read out of a standard PDF."""

    clause: str
    designations: list[str]
    definition: str
    notes: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)

    def to_mapping(self) -> dict:
        data: dict = {
            "term": self.designations[0],
            "definition": self.definition,
            "clause": self.clause,
        }
        if len(self.designations) > 1:
            data["synonyms"] = self.designations[1:]
        if self.notes:
            data["notes"] = self.notes
        if self.examples:
            data["examples"] = self.examples
        return data


@dataclass
class _Line:
    top: int
    left: int
    bold: bool
    text: str


@lru_cache(maxsize=4)
def _pdf_to_xml(path: Path) -> str:
    """Render the PDF text layer to XML.

    Cached because a caller typically wants both the document reference and
    the terms, and these PDFs take seconds each to convert.
    """
    if not shutil.which("pdftohtml"):
        raise RuntimeError(
            "pdftohtml is required to read standard PDFs "
            "(Debian/Ubuntu: apt install poppler-utils)"
        )
    result = subprocess.run(
        ["pdftohtml", "-xml", "-i", "-stdout", str(path)],
        capture_output=True,
        timeout=600,
    )
    if result.returncode != 0 and not result.stdout:
        raise RuntimeError(
            f"pdftohtml failed on {path.name}: "
            f"{result.stderr.decode('utf-8', 'replace')[:200]}"
        )
    return result.stdout.decode("utf-8", "replace")


def _fragments(xml: str) -> list[_Line]:
    """Every positioned text fragment, with watermark and header text dropped."""
    out: list[_Line] = []
    pattern = re.compile(
        r'<text\s+top="(-?\d+)"\s+left="(-?\d+)"[^>]*>(.*?)</text>', re.S
    )
    for match in pattern.finditer(xml):
        fragment = match.group(3)
        text = html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()
        if not text or _WATERMARK.search(text):
            continue
        if _RUNNING_HEADER.match(text):
            continue
        out.append(
            _Line(
                top=int(match.group(1)),
                left=int(match.group(2)),
                bold="<b>" in fragment,
                text=text,
            )
        )
    return out


def _lines(xml: str) -> list[_Line]:
    """Flatten the XML into visual lines.

    A printed line arrives as several fragments: a note label is separate
    from its text, and an italic cross-referenced term is its own fragment.
    Consecutive non-bold fragments level with the current line are joined
    onto it, allowing a couple of pixels of slack because an inline italic
    term is typeset marginally off its neighbours.

    The rule is deliberately narrow. A bold fragment always starts a new
    line, which is what keeps a bold entry number ("3.1") from absorbing the
    bold designation typeset beneath it; widening this was measured to lose
    roughly half the entries across the corpus.

    Note that dropping whole left-hand columns of bare numbers, to remove the
    line numbering in draft PDFs, is *not* safe: page numbers are often set
    at the body text margin, so the column heuristic deletes the body. Draft
    line numbers are left in place instead, and those drafts are not indexed.
    """
    merged: list[_Line] = []
    for fragment in _fragments(xml):
        if (
            merged
            and abs(fragment.top - merged[-1].top) <= _LINE_TOLERANCE
            and not fragment.bold
        ):
            merged[-1].text = _WS.sub(
                " ", f"{merged[-1].text} {fragment.text}"
            ).strip()
            continue
        merged.append(fragment)

    return [
        line
        for line in merged
        if not _BARE_INT.match(line.text) and not _TOC_LEADER.search(line.text)
    ]


#: Only the top and bottom of a page carry running headers. Restricting to
#: those bands matters: the body cites other standards constantly, and those
#: citations match the same pattern.
_HEADER_BAND = 90


def reference_from_filename(path: Path) -> str | None:
    """Best-effort citation from the file name.

    The DIN download names are regular enough to parse, and a name is more
    reliable than a body-text citation when the header band yields nothing.
    """
    stem = path.stem.replace("_", " ")
    match = re.search(
        r"((?:DIN\s+)?(?:EN\s+)?(?:ISO|IEC)(?:[/-](?:IEC|TS|TR|PAS|DIS|FDIS|SPEC))?"
        r"\s*\d+(?:-\d+)?)\s*(\d{4})?",
        stem,
        re.IGNORECASE,
    )
    if not match:
        return None
    designation = _WS.sub(" ", match.group(1)).strip()
    # Normalize the separator ISO uses between body and subtype.
    designation = re.sub(
        r"\b(ISO)[-\s]+(IEC|TS|TR|PAS|DIS|FDIS)\b",
        r"\1/\2",
        designation,
        flags=re.IGNORECASE,
    )
    year = match.group(2)
    return f"{designation}:{year}" if year else designation


def document_reference(path: Path) -> str | None:
    """Recover the document reference, preferring its own running header."""
    counts: Counter[str] = Counter()
    pattern = re.compile(
        r'<text\s+top="(-?\d+)"[^>]*>(.*?)</text>', re.S
    )
    page_height = 0
    for match in re.finditer(r'<page[^>]*height="(\d+)"', _pdf_to_xml(path)):
        page_height = max(page_height, int(match.group(1)))

    for match in pattern.finditer(_pdf_to_xml(path)):
        top = int(match.group(1))
        in_band = top <= _HEADER_BAND or (
            page_height and top >= page_height - _HEADER_BAND
        )
        if not in_band:
            continue
        text = html.unescape(re.sub(r"<[^>]+>", "", match.group(2))).strip()
        if _RUNNING_HEADER.match(text):
            # Drop the language marker: "EN ISO 12100:2010 (E)" is a header
            # decoration, not part of the reference.
            counts[re.sub(r"\s*\([A-Z]\)$", "", text).strip()] += 1

    if counts:
        # A header repeats on most pages; a stray match does not.
        reference, hits = counts.most_common(1)[0]
        if hits >= 3:
            return reference
    return reference_from_filename(path)


#: A pre-definition line no longer than this may be an admitted synonym
#: rather than the start of the definition.
_SYNONYM_MAX = 50


def _split_synonyms(body: list[str]) -> tuple[list[str], list[str]]:
    """Separate admitted synonyms from the definition text.

    Some standards typeset admitted terms in bold, which the caller already
    handles. Others, ISO/IEC 22989 among them, set them in plain text, so
    they arrive looking like the first lines of the definition. They are
    recognizable as short lines that precede a full-width one.

    A short line is only treated as a synonym when a longer line follows it,
    so a genuinely terse definition ("potential source of harm") survives.
    """
    for index, line in enumerate(body):
        if len(line) > _SYNONYM_MAX or line.startswith("<"):
            return body[:index], body[index:]
    return [], body


def _flush(
    clause: str | None,
    designations: list[str],
    definition: list[str],
    notes: list[str],
    examples: list[str],
) -> ExtractedTerm | None:
    if not clause or not designations:
        return None
    extra, body = _split_synonyms(definition)
    text = _clean(" ".join(body))
    if not text:
        return None
    return ExtractedTerm(
        clause=clause,
        designations=designations + [_clean(x) for x in extra if _clean(x)],
        definition=text,
        notes=[_clean(n) for n in notes if _clean(n)],
        examples=[_clean(e) for e in examples if _clean(e)],
    )


def _clean(text: str) -> str:
    """Drop cross-reference numbers and normalize whitespace.

    ISO writes a referenced term as "autonomy (3.2)"; the term belongs in the
    definition, the clause number does not.
    """
    text = _XREF.sub("", text)
    # Soft hyphens and the zero-width joiners ISO uses inside URLs.
    text = text.replace("­", "").replace("​", "")
    return _WS.sub(" ", text).strip()


def extract(path: Path) -> list[ExtractedTerm]:
    """Read every term entry from the terminology clauses of a standard PDF."""
    lines = _lines(_pdf_to_xml(path))

    terms: list[ExtractedTerm] = []
    in_terms = False
    clause_root: str | None = None

    clause: str | None = None
    designations: list[str] = []
    definition: list[str] = []
    notes: list[str] = []
    examples: list[str] = []
    target: str | None = None  # which bucket trailing plain lines extend

    def flush() -> None:
        nonlocal clause, designations, definition, notes, examples, target
        entry = _flush(clause, designations, definition, notes, examples)
        if entry:
            terms.append(entry)
        clause, designations, definition = None, [], []
        notes, examples, target = [], [], None

    for line in lines:
        text = line.text

        if line.bold:
            heading = _CLAUSE_HEADING.match(text)
            if heading and not _ENTRY_NUMBER.match(text):
                number = heading.group(1)
                root = number.split(".")[0]
                if _TERMS_HEADING.match(text):
                    flush()
                    in_terms, clause_root = True, root
                elif in_terms and "." not in number and root != clause_root:
                    # A new top-level clause that is not terminology ends the
                    # terms section.
                    flush()
                    in_terms, clause_root = False, None
                continue

            if in_terms and _ENTRY_NUMBER.match(text):
                flush()
                clause = text
                continue

            if in_terms and clause and not definition:
                # Bold lines between the number and the definition are the
                # preferred term and its synonyms.
                designations.append(text)
                continue
            continue

        if not in_terms or not clause:
            continue

        note = _NOTE.match(text)
        if note:
            notes.append(note.group(1))
            target = "note"
            continue
        example = _EXAMPLE.match(text)
        if example:
            examples.append(example.group(1))
            target = "example"
            continue

        if target == "note" and notes:
            notes[-1] = f"{notes[-1]} {text}"
        elif target == "example" and examples:
            examples[-1] = f"{examples[-1]} {text}"
        elif designations:
            definition.append(text)

    flush()
    return terms


def assert_clean(terms: list[ExtractedTerm]) -> None:
    """Fail loudly if licensee-identifying text survived extraction."""
    for term in terms:
        blob = " ".join(
            [term.definition, *term.notes, *term.examples, *term.designations]
        )
        for token in FORBIDDEN:
            if token.lower() in blob.lower():
                raise RuntimeError(
                    f"watermark text {token!r} leaked into entry {term.clause}"
                )
