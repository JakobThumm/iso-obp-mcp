"""Term normalization and variant generation.

ISO terminology is not written the way papers write it. Entries are often
inverted ("absorption, atmospheric"), carry subject-field qualifiers in
angle brackets ("<robotics> speed"), and append grammatical gender markers
in non-English languages. A writing assistant asking for "atmospheric
absorption" must still find the entry, so every entry is indexed under
several normalized variants.
"""

from __future__ import annotations

import re
import unicodedata

# Angle-bracket subject field, e.g. "<robotics> control" or "〈robotics〉 control".
_DOMAIN_RE = re.compile(r"[<〈‹][^>〉›]{0,60}[>〉›]")
# Trailing parenthetical, e.g. "robot (industrial)".
_PAREN_RE = re.compile(r"\([^)]{0,60}\)")
# Glossarist inline concept links: {{urn:iso:std:...,material entity}} -> "material entity".
_CONCEPT_LINK_RE = re.compile(r"\{\{([^{}]*?)\}\}")
# Some registers carry HTML markup inline in the definition text.
_HTML_TAG_RE = re.compile(r"<[^>]{0,200}>")
_HTML_ENTITY_RE = re.compile(r"&(#\d{1,6}|#x[0-9a-fA-F]{1,6}|[a-zA-Z]{2,10});")
_WS_RE = re.compile(r"\s+")
# Keep intra-word hyphens and apostrophes out of the strip set so that
# "real-time" and "operator's" normalize predictably.
_PUNCT_RE = re.compile(r"[^\w\s\-']", re.UNICODE)


_ENTITIES = {
    "amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'",
    "nbsp": " ", "ndash": "-", "mdash": "-", "deg": "\u00b0",
    "times": "\u00d7", "middot": "\u00b7", "hellip": "...",
}


def _unescape(match: re.Match[str]) -> str:
    name = match.group(1)
    try:
        if name.startswith("#x") or name.startswith("#X"):
            return chr(int(name[2:], 16))
        if name.startswith("#"):
            return chr(int(name[1:]))
    except (ValueError, OverflowError):
        return match.group(0)
    return _ENTITIES.get(name, match.group(0))


def clean_definition(text: str) -> str:
    """Render a Glossarist or STS definition into plain prose.

    Inline concept links carry a URN and a display term; only the display
    term belongs in the readable definition. Some registers additionally
    embed raw HTML in the definition text, which would otherwise be handed
    to the calling agent verbatim.
    """
    if not text:
        return ""

    def _unlink(match: re.Match[str]) -> str:
        # Registers disagree on the order of the two parts. ISO/TC 204 writes
        # "{{urn:iso:std:iso:14812:ed3:3.1.1.3,material entity}}" while the
        # IEV writes "{{functional unit, urn:iec:std:iec:60050-171-01-22}}",
        # so the display term is whichever part is not a URN.
        parts = [part.strip() for part in match.group(1).split(",")]
        display = [part for part in parts if part and not part.startswith("urn:")]
        if display:
            return " ".join(display)
        # A bare cross-reference with no display term, as in the IEV's
        # "See {{urn:iec:std:iec:60050-903-01-19}}". Render the entry it
        # points at, so the definition does not trail off into nothing.
        urns = [part for part in parts if part.startswith("urn:")]
        if not urns:
            return ""
        segments = urns[0].split(":")
        return f"{segments[1].upper()} {segments[-1]}" if len(segments) > 2 else ""

    text = _CONCEPT_LINK_RE.sub(_unlink, text)
    text = _HTML_TAG_RE.sub("", text)
    text = _HTML_ENTITY_RE.sub(_unescape, text)
    return _WS_RE.sub(" ", text).strip()


def normalize(term: str) -> str:
    """Casefold and strip punctuation so lookups survive cosmetic differences."""
    if not term:
        return ""
    text = unicodedata.normalize("NFKC", term)
    text = _DOMAIN_RE.sub(" ", text)
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip().casefold()


def _deinvert(term: str) -> str | None:
    """Turn an inverted ISO designation into natural word order.

    "absorption, atmospheric" -> "atmospheric absorption"

    Only single-comma designations are reordered. Multi-comma entries are
    usually enumerations rather than inversions, and reordering those
    produces nonsense.
    """
    if term.count(",") != 1:
        return None
    head, tail = (part.strip() for part in term.split(","))
    if not head or not tail:
        return None
    # A gender/POS marker rather than an inversion, e.g. "vecteur, m".
    if len(tail) <= 2 or tail.lower() in {"adj", "vb", "verb", "m", "f", "n"}:
        return None
    return f"{tail} {head}"


def variants(term: str) -> list[str]:
    """Normalized forms an entry should be findable under, de-duplicated.

    The first element is always the primary normalization of the designation
    as printed, so callers can distinguish an exact hit from a variant hit.
    """
    if not term:
        return []

    raw = unicodedata.normalize("NFKC", term).strip()
    candidates: list[str] = [raw]

    deinverted = _deinvert(raw)
    if deinverted:
        candidates.append(deinverted)

    # A designation may carry a domain qualifier, a parenthetical, or both;
    # index the bare term too so a plain lookup still matches.
    stripped = _PAREN_RE.sub(" ", _DOMAIN_RE.sub(" ", raw)).strip()
    if stripped and stripped != raw:
        candidates.append(stripped)
        bare_deinverted = _deinvert(stripped)
        if bare_deinverted:
            candidates.append(bare_deinverted)

    out: list[str] = []
    for candidate in candidates:
        norm = normalize(candidate)
        if not norm:
            continue
        if norm not in out:
            out.append(norm)
        # Standards are inconsistent about hyphenating compounds: ISO 8373
        # writes "end-effector" where a paper writes "end effector". Index
        # both so the lookup does not turn on typography.
        unhyphenated = _WS_RE.sub(" ", norm.replace("-", " ")).strip()
        if unhyphenated and unhyphenated not in out:
            out.append(unhyphenated)
    return out
