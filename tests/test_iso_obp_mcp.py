"""Tests for normalization, indexing and lookup."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iso_obp_mcp import db  # noqa: E402
from iso_obp_mcp.ingest import ingest_glossarist, ingest_sts  # noqa: E402
from iso_obp_mcp.normalize import clean_definition, normalize, variants  # noqa: E402


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------


def test_normalize_is_case_and_punctuation_insensitive():
    assert normalize("Coordinate Reference System.") == "coordinate reference system"


def test_normalize_strips_domain_qualifier():
    assert normalize("<robotics> speed") == "speed"
    assert normalize("〈robotics〉 speed") == "speed"


def test_normalize_keeps_intra_word_hyphen():
    assert normalize("real-time control") == "real-time control"


def test_variants_deinverts_a_single_comma_designation():
    assert variants("absorption, atmospheric") == [
        "absorption atmospheric",
        "atmospheric absorption",
    ]


def test_variants_does_not_deinvert_a_gender_marker():
    # "m" is a grammatical gender marker, not an inverted head word.
    assert variants("vecteur du reseau, m") == ["vecteur du reseau m"]


def test_variants_indexes_bare_term_without_parenthetical():
    assert "robot" in variants("robot (industrial)")


def test_variants_are_deduplicated():
    assert len(variants("sensor")) == 1


def test_clean_definition_unwraps_concept_links():
    text = "device observing a {{urn:iso:std:iso:14812:ed3:3.1.1.3,material entity}}"
    assert clean_definition(text) == "device observing a material entity"


def test_clean_definition_strips_html_and_entities():
    text = '<i><a href="#x"><u>material entity</u></a></i> designed to &amp; move'
    assert clean_definition(text) == "material entity designed to & move"


# --------------------------------------------------------------------------
# Index and lookup
# --------------------------------------------------------------------------


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "terms.db")
    yield connection
    connection.close()


def _seed(connection):
    db.replace_source(
        connection,
        slug="demo-a",
        label="ISO 8373:2021",
        standard="ISO 8373:2021",
        entries=[
            db.Entry(
                designation="robot",
                definition="programmed actuated mechanism with a degree of autonomy",
                identifier="3.1",
                clause="3.1",
                synonyms=["manipulating industrial robot"],
                standard="ISO 8373:2021",
            ),
            db.Entry(
                designation="absorption, atmospheric",
                definition="reduction of sound energy by the atmosphere",
                clause="2",
                standard="ISO 8373:2021",
            ),
        ],
    )
    db.replace_source(
        connection,
        slug="demo-b",
        label="ISO 12100:2010",
        standard="ISO 12100:2010",
        entries=[
            db.Entry(
                designation="robot",
                definition="machine that performs work under automatic control",
                clause="3.9",
                standard="ISO 12100:2010",
            )
        ],
    )


def test_lookup_returns_every_standard_that_defines_the_term(conn):
    _seed(conn)
    matches = db.lookup(conn, "robot")
    assert {match["standard"] for match in matches} == {
        "ISO 8373:2021",
        "ISO 12100:2010",
    }


def test_lookup_is_case_insensitive(conn):
    _seed(conn)
    assert db.lookup(conn, "  RoBoT ")


def test_lookup_finds_a_synonym_and_reports_the_preferred_term(conn):
    _seed(conn)
    (match,) = db.lookup(conn, "manipulating industrial robot")
    assert match["term"] == "robot"
    assert match["match_type"] == "synonym"


def test_lookup_finds_a_deinverted_designation(conn):
    _seed(conn)
    (match,) = db.lookup(conn, "atmospheric absorption")
    assert match["term"] == "absorption, atmospheric"


def test_lookup_of_an_unknown_term_is_empty(conn):
    _seed(conn)
    assert db.lookup(conn, "quantum flux capacitor") == []


def test_lookup_of_an_empty_term_is_empty(conn):
    _seed(conn)
    assert db.lookup(conn, "   ") == []


def test_search_tolerates_fts_operators_in_user_input(conn):
    _seed(conn)
    # Unquoted this would be an FTS5 syntax error rather than a result.
    assert db.search(conn, 'robot OR "') is not None


def test_search_finds_a_term_by_words_in_its_definition(conn):
    _seed(conn)
    results = db.search(conn, "automatic control")
    assert any(item["standard"] == "ISO 12100:2010" for item in results)


def test_reingesting_a_source_replaces_rather_than_duplicates(conn):
    _seed(conn)
    _seed(conn)
    assert len(db.lookup(conn, "robot")) == 2
    assert db.corpus_info(conn)["standards_indexed"] == 2


def test_corpus_info_reports_totals(conn):
    _seed(conn)
    info = db.corpus_info(conn)
    assert info["total_entries"] == 3
    assert info["languages"] == ["eng"]


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------


GLOSSARIST_CONCEPT = """
data:
  identifier: '3.1.2'
  localized_concepts:
    eng: 11111111-1111-1111-1111-111111111111
status: valid
schema_version: '3'
id: 00000000-0000-0000-0000-000000000000
---
data:
  definition:
  - content: device observing a {{urn:iso:std:iso:14812:ed3:3.1.1.3,material entity}}
  notes: []
  examples: []
  sources:
  - origin:
      ref:
        source: ISO 14812:2025
      locality:
        type: clause
        reference_from: 3.1.2
      link: https://www.iso.org/standard/1.html
    type: authoritative
  terms:
  - type: expression
    normative_status: deprecated
    designation: transducer
  - type: expression
    normative_status: preferred
    designation: sensor
  language_code: eng
  entry_status: valid
id: 11111111-1111-1111-1111-111111111111
"""


def test_ingest_glossarist_reads_a_register(tmp_path):
    root = tmp_path / "register"
    (root / "concepts").mkdir(parents=True)
    (root / "register.yaml").write_text(
        "id: demo\nref: ISO 14812:2025\nowner: ISO/TC 204\nstatus: current\n",
        encoding="utf-8",
    )
    (root / "concepts" / "a.yaml").write_text(GLOSSARIST_CONCEPT, encoding="utf-8")

    connection = db.connect(tmp_path / "terms.db")
    results = ingest_glossarist(connection, root, slug="demo")
    assert results == [("ISO 14812:2025", 1)]

    (match,) = db.lookup(connection, "sensor")
    # The preferred designation is primary even when listed after a
    # deprecated one, and inline concept links are resolved to plain prose.
    assert match["term"] == "sensor"
    assert match["definition"] == "device observing a material entity"
    assert match["standard"] == "ISO 14812:2025"
    assert match["clause"] == "3.1.2"

    # The deprecated designation is still findable, mapped to the preferred term.
    (deprecated,) = db.lookup(connection, "transducer")
    assert deprecated["term"] == "sensor"
    connection.close()


STS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<standard xmlns:tbx="urn:iso:std:iso:30042:ed-2">
  <front><std-meta><std-ref type="dated">ISO 8373:2021</std-ref></std-meta></front>
  <body>
    <term-sec id="sec_">
      <tbx:termEntry id="term_">
        <tbx:langSet xml:lang="en">
          <tbx:definition>programmed actuated mechanism</tbx:definition>
          <tbx:tig id="term_3.1-1">
            <tbx:term>manipulating industrial robot</tbx:term>
            <tbx:normativeAuthorization value="admittedTerm"/>
          </tbx:tig>
          <tbx:tig id="term_3.1-2">
            <tbx:term>robot</tbx:term>
            <tbx:normativeAuthorization value="preferredTerm"/>
          </tbx:tig>
        </tbx:langSet>
      </tbx:termEntry>
    </term-sec>
  </body>
</standard>
"""


def test_ingest_sts_reads_obp_access_output(tmp_path):
    path = tmp_path / "iso8373.xml"
    path.write_text(STS_XML, encoding="utf-8")

    connection = db.connect(tmp_path / "terms.db")
    label, count = ingest_sts(connection, path)
    assert (label, count) == ("ISO 8373:2021", 1)

    (match,) = db.lookup(connection, "robot")
    # The preferred term wins over the admitted one listed before it, and the
    # clause is recovered from the tig id because termEntry/@id is empty.
    assert match["term"] == "robot"
    assert match["clause"] == "3.1"
    assert match["match_type"] == "exact"
    connection.close()


# --------------------------------------------------------------------------
# Re-ingestion integrity
# --------------------------------------------------------------------------


def test_reingesting_keeps_the_fts_index_consistent(conn):
    """The README tells users to re-run the build, so this must stay sound.

    entries_fts is an external-content FTS5 table keyed on entries.id. If
    stale rows survived a re-ingest, SQLite could pair a recycled rowid with
    another term's definition.
    """
    _seed(conn)
    before = [(item["term"], item["definition"]) for item in db.search(conn, "robot")]

    for _ in range(3):
        _seed(conn)

    after = [(item["term"], item["definition"]) for item in db.search(conn, "robot")]
    assert before == after

    counts = conn.execute(
        "SELECT (SELECT COUNT(*) FROM entries) AS entries, "
        "(SELECT COUNT(*) FROM entries_fts) AS fts"
    ).fetchone()
    assert counts["entries"] == counts["fts"]

    mispaired = conn.execute(
        "SELECT COUNT(*) AS n FROM entries_fts "
        "JOIN entries e ON e.id = entries_fts.rowid "
        "WHERE entries_fts.designation <> e.designation"
    ).fetchone()["n"]
    assert mispaired == 0


# --------------------------------------------------------------------------
# Deprecated designations
# --------------------------------------------------------------------------


def test_lookup_flags_a_deprecated_designation(conn):
    db.replace_source(
        conn,
        slug="demo",
        label="ISO 8373:2021",
        entries=[
            db.Entry(
                designation="collaborative operation",
                definition="state in which a robot system and an operator share a workspace",
                synonyms=["cooperative operation"],
                deprecated_synonyms=["cooperative operation"],
                standard="ISO 8373:2021",
            )
        ],
    )
    (match,) = db.lookup(conn, "cooperative operation")
    assert match["match_type"] == "deprecated_synonym"
    assert match["use_instead"] == "collaborative operation"

    # The preferred term itself is not flagged.
    (preferred,) = db.lookup(conn, "collaborative operation")
    assert preferred["match_type"] == "exact"
    assert "use_instead" not in preferred


def test_ingest_glossarist_marks_a_deprecated_synonym(tmp_path):
    root = tmp_path / "register"
    (root / "concepts").mkdir(parents=True)
    (root / "register.yaml").write_text("id: demo\nref: ISO 14812:2025\n", encoding="utf-8")
    (root / "concepts" / "a.yaml").write_text(GLOSSARIST_CONCEPT, encoding="utf-8")

    connection = db.connect(tmp_path / "terms.db")
    ingest_glossarist(connection, root, slug="demo")
    # "transducer" is marked deprecated in the fixture in favour of "sensor".
    (match,) = db.lookup(connection, "transducer")
    assert match["match_type"] == "deprecated_synonym"
    assert match["use_instead"] == "sensor"
    connection.close()


def test_ingest_sts_marks_a_deprecated_synonym(tmp_path):
    xml = STS_XML.replace(
        '<tbx:term>manipulating industrial robot</tbx:term>\n'
        '            <tbx:normativeAuthorization value="admittedTerm"/>',
        '<tbx:term>manipulating industrial robot</tbx:term>\n'
        '            <tbx:normativeAuthorization value="deprecatedTerm"/>',
    )
    path = tmp_path / "iso8373.xml"
    path.write_text(xml, encoding="utf-8")

    connection = db.connect(tmp_path / "terms.db")
    ingest_sts(connection, path)
    (match,) = db.lookup(connection, "manipulating industrial robot")
    assert match["match_type"] == "deprecated_synonym"
    assert match["use_instead"] == "robot"
    connection.close()


# --------------------------------------------------------------------------
# Split-layout registers (IEV)
# --------------------------------------------------------------------------


def test_ingest_glossarist_reads_a_split_layout_register(tmp_path):
    """The IEV dataset stores each localization in its own file."""
    root = tmp_path / "iev"
    (root / "concepts" / "concept").mkdir(parents=True)
    (root / "concepts" / "localized_concept").mkdir(parents=True)
    (root / "register.yaml").write_text(
        "id: iev\nname: IEC Electropedia (IEV)\n", encoding="utf-8"
    )
    (root / "concepts" / "concept" / "351-42-06.yaml").write_text(
        "data:\n"
        "  identifier: 351-42-06\n"
        "  localized_concepts:\n"
        "    eng: aaaa\n"
        "    deu: bbbb\n"
        "status: valid\n"
        "id: 351-42-06\n",
        encoding="utf-8",
    )
    (root / "concepts" / "localized_concept" / "aaaa.yaml").write_text(
        "data:\n"
        "  definition:\n"
        "  - content: system in which control is exercised automatically\n"
        "  terms:\n"
        "  - type: expression\n"
        "    normative_status: preferred\n"
        "    designation: automatic control system\n"
        "  language_code: eng\n"
        "id: aaaa\n",
        encoding="utf-8",
    )
    (root / "concepts" / "localized_concept" / "bbbb.yaml").write_text(
        "data:\n"
        "  definition:\n"
        "  - content: System mit automatischer Regelung\n"
        "  terms:\n"
        "  - type: expression\n"
        "    normative_status: preferred\n"
        "    designation: automatisches Regelungssystem\n"
        "  language_code: deu\n"
        "id: bbbb\n",
        encoding="utf-8",
    )

    connection = db.connect(tmp_path / "terms.db")
    results = ingest_glossarist(connection, root, slug="iev", languages={"eng"})
    # The language filter keeps the German localization out.
    assert results == [("IEC Electropedia (IEV)", 1)]

    (match,) = db.lookup(connection, "automatic control system")
    assert match["definition"] == "system in which control is exercised automatically"
    # The standard is derived from the IEV part number in the identifier.
    assert match["standard"] == "IEC 60050-351"
    assert match["clause"] == "351-42-06"
    assert db.lookup(connection, "automatisches Regelungssystem", language="deu") == []
    connection.close()


def test_clean_definition_handles_both_concept_link_orders():
    # ISO/TC 204 puts the URN first; the IEV puts the display term first.
    iso = "device observing a {{urn:iso:std:iso:14812:ed3:3.1.1.3,material entity}}"
    iev = "capability of a {{functional unit, urn:iec:std:iec:60050-171-01-22}} to act"
    assert clean_definition(iso) == "device observing a material entity"
    assert clean_definition(iev) == "capability of a functional unit to act"


def test_clean_definition_renders_a_bare_cross_reference():
    # An IEV "See <entry>" link carries no display term; dropping it would
    # leave the definition as the single word "See".
    assert (
        clean_definition("See {{urn:iec:std:iec:60050-903-01-19}}")
        == "See IEC 60050-903-01-19"
    )


def test_ingest_split_layout_survives_a_malformed_localization(tmp_path):
    root = tmp_path / "iev"
    (root / "concepts" / "concept").mkdir(parents=True)
    (root / "concepts" / "localized_concept").mkdir(parents=True)
    (root / "register.yaml").write_text("id: iev\nname: IEV\n", encoding="utf-8")
    (root / "concepts" / "concept" / "1-1-1.yaml").write_text(
        "data:\n  identifier: 1-1-1\n  localized_concepts:\n"
        "    eng: good\n    deu: empty\n    fra: missing\nid: 1-1-1\n",
        encoding="utf-8",
    )
    (root / "concepts" / "localized_concept" / "good.yaml").write_text(
        "data:\n  definition:\n  - content: a definition\n  terms:\n"
        "  - designation: a term\n    normative_status: preferred\n"
        "  language_code: eng\nid: good\n",
        encoding="utf-8",
    )
    # A bare "data:" key parses to None, and the French file is absent.
    (root / "concepts" / "localized_concept" / "empty.yaml").write_text(
        "data:\nid: empty\n", encoding="utf-8"
    )

    connection = db.connect(tmp_path / "terms.db")
    assert ingest_glossarist(connection, root, slug="iev") == [("IEV", 1)]
    assert db.lookup(connection, "a term")[0]["definition"] == "a definition"
    connection.close()


# --------------------------------------------------------------------------
# User glossary
# --------------------------------------------------------------------------


from iso_obp_mcp import glossary  # noqa: E402
from iso_obp_mcp.ingest import ingest_user_glossary  # noqa: E402


@pytest.fixture()
def gloss(tmp_path):
    return tmp_path / "glossary.yaml"


def test_glossary_round_trips_both_kinds_of_entry(tmp_path, gloss):
    gloss.write_text(
        "terms:\n"
        "  - term: machine learning\n"
        "    definition: process of optimizing model parameters\n"
        "    standard: ISO/IEC 22989:2022\n"
        "    clause: '3.1.8'\n"
        "    synonyms: [ML]\n"
        "  - term: shared autonomy\n"
        "    definition: operator and system jointly determine action\n",
        encoding="utf-8",
    )
    connection = db.connect(tmp_path / "terms.db")
    _, count = ingest_user_glossary(connection, gloss)
    assert count == 2

    (transcribed,) = db.lookup(connection, "machine learning")
    assert transcribed["authority"] == db.AUTHORITY_TRANSCRIBED
    assert transcribed["standard"] == "ISO/IEC 22989:2022"
    assert transcribed["clause"] == "3.1.8"
    # It cites a standard, but the wording was typed in rather than extracted.
    assert transcribed["from_standard"] is True
    assert transcribed["verbatim_from_source"] is False

    # A synonym of a user entry is findable too.
    assert db.lookup(connection, "ML")[0]["term"] == "machine learning"

    (own,) = db.lookup(connection, "shared autonomy")
    assert own["authority"] == db.AUTHORITY_USER
    # A user's own definition must not claim a standard.
    assert own["standard"] is None
    assert own["from_standard"] is False
    connection.close()


def test_a_user_definition_never_outranks_a_standard(tmp_path, gloss):
    """A user coining a term a standard already defines must not shadow it."""
    connection = db.connect(tmp_path / "terms.db")
    db.replace_source(
        connection,
        slug="register",
        label="ISO 8373:2021",
        entries=[
            db.Entry(
                designation="robot",
                definition="programmed actuated mechanism",
                standard="ISO 8373:2021",
                authority=db.AUTHORITY_REGISTER,
            )
        ],
    )
    gloss.write_text(
        "terms:\n  - term: robot\n    definition: my own take on robots\n",
        encoding="utf-8",
    )
    ingest_user_glossary(connection, gloss)

    matches = db.lookup(connection, "robot")
    assert len(matches) == 2
    # The published definition sorts first regardless of NULL standards.
    assert matches[0]["authority"] == db.AUTHORITY_REGISTER
    assert matches[1]["authority"] == db.AUTHORITY_USER
    connection.close()


def test_reingesting_a_register_leaves_user_terms_alone(tmp_path, gloss):
    """build-index.sh re-runs the registers; it must not drop your glossary."""
    connection = db.connect(tmp_path / "terms.db")
    gloss.write_text(
        "terms:\n  - term: shared autonomy\n    definition: my definition\n",
        encoding="utf-8",
    )
    ingest_user_glossary(connection, gloss)

    for _ in range(2):
        db.replace_source(
            connection,
            slug="register",
            label="ISO 8373:2021",
            entries=[db.Entry(designation="robot", definition="a mechanism")],
        )

    assert db.lookup(connection, "shared autonomy")
    assert db.corpus_info(connection)["entries_added_by_user"] == 1
    connection.close()


def test_adding_a_term_preserves_hand_written_comments(gloss):
    gloss.write_text(
        "# My notes, which must survive.\nterms:\n"
        "  # an existing entry\n"
        "  - term: first\n    definition: one\n",
        encoding="utf-8",
    )
    glossary.add(glossary.Term(term="second", definition="two"), gloss)

    text = gloss.read_text(encoding="utf-8")
    assert "# My notes, which must survive." in text
    assert "# an existing entry" in text
    assert [term.term for term in glossary.load(gloss)] == ["first", "second"]


def test_adding_a_term_to_an_empty_template_works(gloss):
    glossary.ensure(gloss)
    assert glossary.load(gloss) == []
    glossary.add(glossary.Term(term="only", definition="one"), gloss)
    assert [term.term for term in glossary.load(gloss)] == ["only"]


def test_removing_prefers_the_primary_term_over_a_synonym(gloss):
    gloss.write_text(
        "terms:\n"
        "  - term: end effector\n    definition: a device\n    synonyms: [gripper]\n"
        "  - term: gripper\n    definition: a different entry\n",
        encoding="utf-8",
    )
    _, removed = glossary.remove("gripper", gloss)
    # The entry named "gripper" goes; "end effector" keeps its synonym.
    assert removed == ["gripper"]
    assert [term.term for term in glossary.load(gloss)] == ["end effector"]


def test_removing_by_synonym_reports_the_entry_it_removed(gloss):
    gloss.write_text(
        "terms:\n"
        "  - term: end effector\n    definition: a device\n    synonyms: [gripper]\n",
        encoding="utf-8",
    )
    _, removed = glossary.remove("gripper", gloss)
    assert removed == ["end effector"]
    assert glossary.load(gloss) == []


def test_removing_an_absent_term_changes_nothing(gloss):
    gloss.write_text("terms:\n  - term: kept\n    definition: one\n", encoding="utf-8")
    _, removed = glossary.remove("absent", gloss)
    assert removed == []
    assert [term.term for term in glossary.load(gloss)] == ["kept"]


def test_glossary_rejects_an_entry_without_a_definition(gloss):
    gloss.write_text("terms:\n  - term: incomplete\n", encoding="utf-8")
    with pytest.raises(glossary.GlossaryError, match="definition"):
        glossary.load(gloss)


def test_glossary_rejects_invalid_yaml(gloss):
    gloss.write_text("terms: [unclosed\n", encoding="utf-8")
    with pytest.raises(glossary.GlossaryError, match="not valid YAML"):
        glossary.load(gloss)


def test_a_missing_glossary_is_empty_rather_than_an_error(tmp_path):
    assert glossary.load(tmp_path / "absent.yaml") == []


def test_glossary_accepts_a_bare_list(gloss):
    gloss.write_text("- term: terse\n  definition: one\n", encoding="utf-8")
    assert [term.term for term in glossary.load(gloss)] == ["terse"]


def test_adding_a_term_matches_existing_list_indentation(gloss):
    """An indented list must stay parseable after an append."""
    gloss.write_text(
        "# header\nterms:\n  - term: first\n    definition: one\n", encoding="utf-8"
    )
    glossary.add(glossary.Term(term="second", definition="two"), gloss)
    text = gloss.read_text(encoding="utf-8")
    assert "# header" in text
    assert "\n  - term: second\n" in text
    assert [t.term for t in glossary.load(gloss)] == ["first", "second"]


def test_adding_a_term_to_an_unindented_list_works(gloss):
    gloss.write_text(
        "# header\nterms:\n- term: first\n  definition: one\n", encoding="utf-8"
    )
    glossary.add(glossary.Term(term="second", definition="two"), gloss)
    assert "# header" in gloss.read_text(encoding="utf-8")
    assert [t.term for t in glossary.load(gloss)] == ["first", "second"]


def test_adding_a_term_records_every_field(gloss):
    glossary.add(
        glossary.Term(
            term="power and force limiting",
            definition="means of limiting robot power and force",
            standard="ISO/TS 15066:2016",
            clause="3.3",
            synonyms=["PFL"],
            notes=["Relevant to collaborative operation."],
            source_note="typed from my copy of the TS",
        ),
        gloss,
    )
    (term,) = glossary.load(gloss)
    assert term.standard == "ISO/TS 15066:2016"
    assert term.clause == "3.3"
    assert term.synonyms == ["PFL"]
    assert term.notes == ["Relevant to collaborative operation."]
    assert term.source_note == "typed from my copy of the TS"


def test_search_ranks_standards_above_user_definitions(tmp_path, gloss):
    """search_terms suggestions must not be led by a user's own wording."""
    connection = db.connect(tmp_path / "terms.db")
    db.replace_source(
        connection,
        slug="register",
        label="IEC 60050-351",
        entries=[
            db.Entry(
                designation="control system",
                definition="system constituted by a controlled system and its controller",
                standard="IEC 60050-351",
                authority=db.AUTHORITY_REGISTER,
            )
        ],
    )
    gloss.write_text(
        "terms:\n  - term: my control system\n    definition: a controlled system\n",
        encoding="utf-8",
    )
    ingest_user_glossary(connection, gloss)

    results = db.search(connection, "controlled system")
    assert len(results) == 2
    # A short user entry would otherwise win on bm25 alone.
    assert results[0]["authority"] == db.AUTHORITY_REGISTER
    connection.close()


def test_connect_can_be_opened_for_cross_thread_use(tmp_path):
    """The server reaches one connection from several worker threads."""
    import threading

    path = tmp_path / "terms.db"
    connection = db.connect(path, check_same_thread=False)
    db.replace_source(
        connection,
        slug="register",
        label="ISO 8373:2021",
        entries=[db.Entry(designation="robot", definition="a mechanism")],
    )

    lock = threading.RLock()
    results: list[int] = []
    errors: list[Exception] = []

    def worker():
        try:
            with lock:
                results.append(len(db.lookup(connection, "robot")))
        except Exception as exc:  # pragma: no cover - only on regression
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert results == [1] * 8
    connection.close()


# --------------------------------------------------------------------------
# Standard PDF extraction
# --------------------------------------------------------------------------


from iso_obp_mcp import pdf as pdfmod  # noqa: E402


def _xml(body: str) -> str:
    return f'<pdf2xml><page number="1" height="1200" width="900">{body}</page></pdf2xml>'


def _t(top: int, left: int, text: str, bold: bool = False) -> str:
    inner = f"<b>{text}</b>" if bold else text
    return f'<text top="{top}" left="{left}" width="10" height="10">{inner}</text>'


def test_extract_reads_entries_from_a_terms_clause(tmp_path, monkeypatch):
    xml = _xml(
        _t(100, 55, "3  Terms and definitions", bold=True)
        + _t(130, 55, "3.1", bold=True)
        + _t(150, 55, "machinery", bold=True)
        + _t(170, 55, "machine", bold=True)
        + _t(190, 55, "assembly fitted with a drive system")
        + _t(210, 55, "NOTE 1")
        + _t(210, 127, "The term also covers an assembly of machines.")
        + _t(240, 55, "3.2", bold=True)
        + _t(260, 55, "reliability", bold=True)
        + _t(280, 55, "ability to perform a required function")
        + _t(320, 55, "4  Strategy for risk reduction", bold=True)
        + _t(340, 55, "4.1", bold=True)
        + _t(360, 55, "this is not a term entry")
    )
    monkeypatch.setattr(pdfmod, "_pdf_to_xml", lambda path: xml)
    terms = pdfmod.extract(tmp_path / "x.pdf")

    assert [t.clause for t in terms] == ["3.1", "3.2"]
    assert terms[0].designations == ["machinery", "machine"]
    assert terms[0].definition == "assembly fitted with a drive system"
    assert terms[0].notes == ["The term also covers an assembly of machines."]
    # Clause 4 is not terminology, so its numbered content is ignored.
    assert all("not a term entry" not in t.definition for t in terms)


def test_extract_stops_at_a_non_terminology_clause(tmp_path, monkeypatch):
    """ISO 12100 numbers list items in its risk clauses the same way."""
    xml = _xml(
        _t(100, 55, "3  Terms and definitions", bold=True)
        + _t(130, 55, "3.1", bold=True)
        + _t(150, 55, "hazard", bold=True)
        + _t(170, 55, "potential source of harm")
        + _t(200, 55, "5  Risk assessment", bold=True)
        + _t(230, 55, "5.1", bold=True)
        + _t(250, 55, "spurious", bold=True)
        + _t(270, 55, "this must not be indexed")
    )
    monkeypatch.setattr(pdfmod, "_pdf_to_xml", lambda path: xml)
    terms = pdfmod.extract(tmp_path / "x.pdf")
    assert len(terms) == 1
    assert terms[0].designations == ["hazard"]


def test_extract_drops_the_licensee_watermark(tmp_path, monkeypatch):
    xml = _xml(
        _t(100, 55, "3  Terms and definitions", bold=True)
        + _t(130, 55, "3.1", bold=True)
        + _t(150, 55, "robot", bold=True)
        + _t(170, 55, "programmed actuated mechanism")
        + _t(190, 55, "Firmenname: Example Institution")
        + _t(200, 55, "Benutzername: _ip_user_00000000-0000")
        + _t(210, 55, "Printed copies are uncontrolled")
        + _t(220, 55, "Datum / Uhrzeit des Ausdrucks: 2026-09-29, 12:32:55")
    )
    monkeypatch.setattr(pdfmod, "_pdf_to_xml", lambda path: xml)
    terms = pdfmod.extract(tmp_path / "x.pdf")
    # The assertion is the safety net; it must not raise.
    pdfmod.assert_clean(terms)
    blob = " ".join([terms[0].definition, *terms[0].notes])
    for token in pdfmod.FORBIDDEN:
        assert token.lower() not in blob.lower()


def test_assert_clean_raises_when_watermark_text_survives():
    leaked = pdfmod.ExtractedTerm(
        clause="3.1",
        designations=["robot"],
        definition="mechanism Benutzername: _ip_user_abc",
    )
    with pytest.raises(RuntimeError, match="leaked"):
        pdfmod.assert_clean([leaked])


def test_extract_separates_non_bold_admitted_synonyms(tmp_path, monkeypatch):
    """ISO/IEC 22989 sets admitted terms in plain text, not bold."""
    xml = _xml(
        _t(100, 55, "3  Terms and definitions", bold=True)
        + _t(130, 55, "3.4.8", bold=True)
        + _t(150, 55, "neural network", bold=True)
        + _t(170, 55, "neural net")
        + _t(190, 55, "artificial neural network")
        + _t(210, 55, "network of one or more layers of neurons connected by links")
    )
    monkeypatch.setattr(pdfmod, "_pdf_to_xml", lambda path: xml)
    (term,) = pdfmod.extract(tmp_path / "x.pdf")
    assert term.designations == [
        "neural network",
        "neural net",
        "artificial neural network",
    ]
    assert term.definition.startswith("network of one or more layers")


def test_a_short_definition_is_not_mistaken_for_a_synonym(tmp_path, monkeypatch):
    xml = _xml(
        _t(100, 55, "3  Terms and definitions", bold=True)
        + _t(130, 55, "3.1", bold=True)
        + _t(150, 55, "hazard", bold=True)
        + _t(170, 55, "potential source of harm")
    )
    monkeypatch.setattr(pdfmod, "_pdf_to_xml", lambda path: xml)
    (term,) = pdfmod.extract(tmp_path / "x.pdf")
    assert term.designations == ["hazard"]
    assert term.definition == "potential source of harm"


def test_extract_joins_fragments_a_pixel_apart(tmp_path, monkeypatch):
    """An inline italic cross-reference sits marginally off its own line."""
    xml = _xml(
        _t(100, 55, "3  Terms and definitions", bold=True)
        + _t(130, 55, "3.1", bold=True)
        + _t(150, 55, "machine learning", bold=True)
        + _t(170, 55, "process of optimizing model parameters")
        + _t(171, 320, "through computational techniques")
    )
    monkeypatch.setattr(pdfmod, "_pdf_to_xml", lambda path: xml)
    (term,) = pdfmod.extract(tmp_path / "x.pdf")
    assert term.definition == (
        "process of optimizing model parameters through computational techniques"
    )


def test_extract_strips_cross_reference_numbers(tmp_path, monkeypatch):
    xml = _xml(
        _t(100, 55, "3  Terms and definitions", bold=True)
        + _t(130, 55, "3.1", bold=True)
        + _t(150, 55, "robot", bold=True)
        + _t(170, 55, "mechanism with a degree of autonomy")
        + _t(170, 300, "(3.2)")
        + _t(171, 340, "to perform tasks")
    )
    monkeypatch.setattr(pdfmod, "_pdf_to_xml", lambda path: xml)
    (term,) = pdfmod.extract(tmp_path / "x.pdf")
    assert term.definition == "mechanism with a degree of autonomy to perform tasks"


def test_extract_ignores_the_table_of_contents(tmp_path, monkeypatch):
    xml = _xml(
        _t(60, 55, "3 Terms and definitions .................... 4")
        + _t(100, 55, "3  Terms and definitions", bold=True)
        + _t(130, 55, "3.1", bold=True)
        + _t(150, 55, "robot", bold=True)
        + _t(170, 55, "programmed actuated mechanism")
    )
    monkeypatch.setattr(pdfmod, "_pdf_to_xml", lambda path: xml)
    assert len(pdfmod.extract(tmp_path / "x.pdf")) == 1


def test_reference_from_filename_handles_the_naming_conventions(tmp_path):
    cases = {
        "ISO-IEC 22989_2022-07-00_EN_3377014.pdf": "ISO/IEC 22989:2022",
        "DIN EN ISO 10218-2_2024-04.pdf": "DIN EN ISO 10218-2:2024",
        "ISO 8373_2021-11-00_EN_3317224.pdf": "ISO 8373:2021",
    }
    for name, expected in cases.items():
        assert pdfmod.reference_from_filename(tmp_path / name) == expected
