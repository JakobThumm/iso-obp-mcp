# iso-obp-mcp

An MCP server that looks up ISO definitions of scientific terms. It is built
for a writing-assistance agent that walks a paper and checks, for every
scientific term, whether a clear ISO definition exists.

Give it a term, and it returns the definition together with the standard and
clause it comes from, or `"Not Defined"`.

## Why the index is local

The [ISO Online Browsing Platform](https://www.iso.org/obp/ui/) (OBP) has no
public search API, and `cdb.iso.org` redirects to the same web UI. The OBP is
also behind Cloudflare bot protection: an automated request gets a `403` JS
challenge rather than content. Bulk automated fetching is therefore neither
possible nor appropriate, so this server answers from a local index that you
populate from openly published sources.

There are two ways to fill it.

### 1. Glossarist registers (the main path, no scraping)

Several ISO committees publish their terminology databases openly as
[Glossarist](https://www.glossarist.org/) YAML. `scripts/build-index.sh`
clones and indexes them:

| Register | Coverage | English entries |
| --- | --- | --- |
| [`glossarist/glossarist-data-iev`](https://github.com/glossarist/glossarist-data-iev) | IEC Electropedia, the International Electrotechnical Vocabulary (IEC 60050). Strong on control, measurement, dependability and electrotechnical terms | ~22 200 |
| [`geolexica/isotc211-glossary`](https://github.com/geolexica/isotc211-glossary) | ISO/TC 211 Multi-Lingual Glossary of Terms (geographic information, ~60 standards) | ~8 750, and 15 further languages |
| [`geolexica/isotc204-glossary`](https://github.com/geolexica/isotc204-glossary) | ISO 14812 intelligent transport systems vocabulary, three editions | ~1 000 |

Each entry keeps its source standard, clause number and, where the register
records one, an `iso.org` link, so a lookup result is citable.

Two notes on the defaults. The IEV is IEC rather than ISO; it is included
because it is the largest openly published standards vocabulary and its
`standard` field names the exact IEC 60050 part, so you can always see what a
definition came from. Set `ISO_OBP_SKIP_IEV=1` to leave it out. English only
is indexed by default; set `ISO_OBP_LANGUAGES=eng,deu,fra` for more.

### 2. Standards you add yourself from the OBP

For a standard outside those registers, open its OBP page in your own
browser, save the "Terms and definitions" preview as HTML, and convert it
with [`metanorma/obp-access`](https://github.com/metanorma/obp-access):

```bash
scripts/obp-html-to-sts.sh iso8373.html \
  iso:std:iso:8373:ed-3:v1:en "ISO 8373:2021" "Robotics - Vocabulary"
```

The script runs `obp-access` in a `ruby:3.3` container, because the gem needs
Ruby >= 3.1, converts the page to NISO STS XML, and indexes the `tbx:termEntry`
records it contains.

Two things to keep in mind. This is a manual, per-standard step by design: it
is you reading pages you opened, not a crawler. And the
[OBP Terms of Use](https://www.iso.org/home/store/iso-obp-terms.html) govern
reuse of OBP content, so whether to keep a local research cache is your call.

[docs/add-standards.md](docs/add-standards.md) lists the robotics and AI
standards worth adding, with the URN and OBP link for each.

### 3. Standards you hold a licensed PDF of

For a standard you have bought or have institutional access to, the terms
clause can be read straight out of the PDF:

```bash
iso-obp-index pdf "DIN EN ISO 10218-2_2024-04.pdf"
```

This writes a reviewable YAML file into `$ISO_OBP_DATA/standards/` and then
indexes it. Extraction is separate from indexing on purpose: the result is
read from a PDF text layer, so it is worth looking at before it starts
answering lookups. Use `--extract-only` to stop after writing the file, and
`--standard` to override the document reference.

The parser keys off the typography ISO uses: a bold clause number on its own
line starts an entry, the bold lines after it are the designations, and the
plain text that follows is the definition. Only clauses whose heading names
terminology are read.

Standard PDFs from a distributor are watermarked with the licensee's
identity. Those lines are stripped, and the extractor refuses to write a file
if any survive.

### 4. Terms you add yourself

Two cases: a standard you have access to that is not in the index, and a term
you want to establish as common knowledge for your own writing. Both live in
one YAML file you own and can version.

```bash
iso-obp-index user --init      # create the file
$EDITOR ~/.config/iso-obp-mcp/glossary.yaml
iso-obp-index user             # re-index after editing
```

```yaml
terms:
  # Transcribed from a standard you have access to: cites its source.
  - term: end effector
    definition: device attached to the mechanical interface to perform the task
    standard: ISO 8373:2021
    clause: "3.13"
    synonyms: [gripper]

  # Your own working definition: no standard, and never reported as one.
  - term: shared autonomy
    definition: control scheme in which an operator and an autonomous system
      jointly determine robot action
    notes:
      - Introduced explicitly in our assistive teleoperation work.
```

Naming a `standard:` is what separates the two. Override the file location
with `ISO_OBP_GLOSSARY`.

You can also add terms through the server, which writes the same file and
reindexes, so nothing is lost on the next re-ingest:

- `define_term(term, definition, standard=..., clause=..., synonyms=[...])`
- `remove_term(term)`
- `list_my_terms()`

The file stays the source of truth. Re-running `scripts/build-index.sh`
refreshes the registers and leaves your glossary untouched.

## Install

```bash
uv venv
uv pip install --python .venv/bin/python -e .
export ISO_OBP_DATA=~/path/to/your-data-dir
scripts/build-index.sh
```

`ISO_OBP_DATA` keeps the index, your glossary and any extracted standards
together in one directory, which is worth pointing at a private repository:
anything read out of a licensed PDF lives there.

| What | Location |
| --- | --- |
| Index | `$ISO_OBP_DB`, else `$ISO_OBP_DATA/terms.db`, else `$XDG_DATA_HOME/iso-obp-mcp/terms.db` |
| Your glossary | `$ISO_OBP_GLOSSARY`, else `$ISO_OBP_DATA/glossary.yaml`, else `$XDG_CONFIG_HOME/iso-obp-mcp/glossary.yaml` |
| Extracted standards | `$ISO_OBP_DATA/standards/*.yaml` |

Re-running the build refreshes each register in place and leaves your own
data alone. `iso-obp-index standards` re-indexes every extracted file.

## Register with Claude Code

```bash
claude mcp add iso-obp --scope user \
  -e ISO_OBP_DATA=/absolute/path/to/your-data-dir \
  -- /absolute/path/to/iso-obp-mcp/.venv/bin/iso-obp-mcp
```

Or add it to `.mcp.json`:

```json
{
  "mcpServers": {
    "iso-obp": {
      "command": "/absolute/path/to/iso-obp-mcp/.venv/bin/python",
      "args": ["-m", "iso_obp_mcp.server"]
    }
  }
}
```

## Tools

| Tool | Purpose |
| --- | --- |
| `lookup_term(term, language="eng")` | Definition of one term, or `"Not Defined"` |
| `lookup_terms(terms, language="eng")` | Batch lookup, for checking a whole paper in one call |
| `search_terms(query, language="eng", limit=10)` | Keyword search, for when an exact lookup misses |
| `corpus_info()` | Exactly which standards the index covers |
| `define_term(...)` | Add one of your own terms, or one transcribed from a standard |
| `remove_term(term)` | Remove a term from your glossary |
| `list_my_terms()` | Review everything you have added |

### How results are shaped

**Every match is returned, not just one.** The same term is often defined
differently by different standards, and that disagreement is the interesting
part when choosing terminology. `lookup_term("robot")` reports both the
ISO 8373 and the ISO 12100 definition and lets the caller decide.

**Lookups tolerate how papers actually write terms.** Matching is
case- and punctuation-insensitive. ISO designations are indexed under several
variants: inverted entries are also indexed in natural order, so
`"atmospheric absorption"` finds `"absorption, atmospheric"`; subject-field
qualifiers and parentheticals are stripped, so `"speed"` finds
`"<robotics> speed"`.

**Deprecated terminology is called out.** Synonyms resolve to the preferred
term and `match_type` says which kind of match it was: `exact`, `synonym`, or
`deprecated_synonym`. A paper writing "cooperative operation" gets the
definition plus `"use_instead": "collaborative operation"`, which is usually
the more useful answer than the definition alone.

**A definition you added is never passed off as a standard's.** This is the
point of the `status` field, and it is graded rather than boolean:

| `status` | Meaning |
| --- | --- |
| `defined` | A standard in the index defines the term. The only status that means this |
| `defined_by_your_transcription` | Only your transcription defines it. It cites a standard and clause, but the wording is unverified |
| `defined_by_you_only` | No standard defines it. This is your own convention, and should be introduced explicitly in the text |
| `not_defined` | Nothing in the index defines it |

Each match also carries `authority` (`register`, `standard-preview`,
`user-transcribed`, `user-defined`), plus `from_standard`,
`verbatim_from_source` and `user_supplied` so a caller can be precise without
parsing the authority string. A user-defined entry reports `"standard": null`
rather than borrowing a corpus label. Where a standard and your glossary both
define a term, both are returned and the standard sorts first.

**`"Not Defined"` is always qualified.** Every response carries a `searched`
field naming the indexed standards. A negative result means the term is
absent from *those* corpora, not that ISO defines it nowhere, and the response
says so. Near-miss `suggestions` come along with it, so a caller can tell "ISO
has no such concept" from "ISO words it differently". This matters: the index
covers a few thousand entries out of ISO's ~9 100 published deliverables, so an
unqualified "Not Defined" would be actively misleading.

### Example

```jsonc
// lookup_term("coordinate reference system")
{
  "status": "defined",
  "match_count": 1,
  "definitions": [{
    "term": "coordinate reference system",
    "definition": "coordinate system that is related to an object by a datum",
    "standard": "ISO 19111:2019",
    "clause": "3.1.9",
    "match_type": "exact",
    "notes": ["For geodetic and vertical datums, the object will be the Earth."],
    "link": "https://www.iso.org/standard/74039.html"
  }],
  "searched": { "entries": 9757, "standards_indexed": 4, "sources": ["..."] }
}
```

## Adding another register

Any Glossarist v3 repository works. Append it to `REGISTERS` in
`scripts/build-index.sh`, or index a clone directly:

```bash
iso-obp-index glossarist ~/src/some-glossary --slug some-glossary
iso-obp-index info
```

Both root layouts are handled: `concepts/` at the repository root, and one
register per edition under `datasets/<name>/concepts/`.

## Development

```bash
.venv/bin/python -m pytest tests/ -q
```

## Limitations

- Coverage is only what you index. The default registers are strong on
  electrotechnical, control, geographic-information and intelligent-transport
  terminology. They have no entry for machine-learning vocabulary such as
  "neural network", and none for robotics terms defined in ISO 8373,
  ISO 10218 or ISO/TS 15066, which are not published as open registers and so
  have to be added through the OBP route above. Check `corpus_info()` before
  trusting a negative.
- Matching is exact-after-normalization, not semantic. A paraphrase will not
  match; `search_terms` is the fallback for that.
- PDF extraction is heuristic. It reads final ISO and DIN adoptions well; it
  does not handle ISO/DIS drafts, whose margin line numbering runs through the
  text, and it reads only the language the PDF is written in. Check the
  extracted YAML for a standard you intend to rely on.
- ISO 80000 quantity tables carry terms in tables rather than `tbx:termEntry`
  records. `obp-access` exposes those through `to_terms`, which this ingester
  does not yet read.
- Definitions are flattened to plain text, so subscripts, superscripts and
  inline formulas lose their markup.
