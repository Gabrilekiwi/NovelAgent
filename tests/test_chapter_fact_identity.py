"""Identity decisions are deterministic and do not fabricate state baselines."""
import copy

import pytest

from core.engine.chapter_fact_identity import (
    ChapterFactIdentityError, normalize_entity_directory,
    prepare_identity_decisions, resolve_entity_reference,
)
from core.engine.chapter_fact_context import build_chapter_fact_context
from core.story_project.paths import infer_next_chapter
from tests.test_reviewed_chapter_commit import case, ready


@pytest.fixture
def directory():
    return [
        {"kind":"character", "entity_id":"char_zhou_lan", "name":"周岚", "aliases":["小周"]},
        {"kind":"character", "entity_id":"char_zhou_ning", "name":"周宁", "aliases":["小周"]},
        {"kind":"location", "entity_id":"location_station", "name":"旧城站", "aliases":["车站"]},
        {"kind":"item", "entity_id":"item_beacon", "name":"信标", "aliases":["A-01"]},
    ]


def declare(key="new_guard", kind="character", name="赵原", aliases=None):
    return {"local_key":key, "kind":kind, "action":"declare", "review_source":"author",
        "name":name, "aliases":aliases or [], "reason":"作者明确确认本章首次出现的实体"}


def plan(directory, decisions, book_id="book-one"):
    return prepare_identity_decisions(directory=directory, decisions=decisions,
        book_id=book_id, review_scope="persisted-review-one")


def test_exact_existing_alias_reuses_typed_id_without_changing_directory(directory):
    before = copy.deepcopy(directory)
    item = resolve_entity_reference(directory, kind="item", reference="Ａ-０１")
    assert item["entity_id"] == "item_beacon"
    item["aliases"].append("external mutation")
    assert directory == before
    with pytest.raises(ChapterFactIdentityError, match="identity_unknown"):
        resolve_entity_reference(directory, kind="character", reference="车站")
    with pytest.raises(ChapterFactIdentityError, match="identity_unknown"):
        resolve_entity_reference(directory, kind="item", reference="A01")


def test_ambiguous_alias_preserves_all_candidates_and_requires_explicit_choice(directory):
    with pytest.raises(ChapterFactIdentityError, match="identity_ambiguous") as caught:
        resolve_entity_reference(directory, kind="character", reference="小周")
    assert {entry["entity_id"] for entry in caught.value.candidates} == {"char_zhou_lan", "char_zhou_ning"}
    selected = resolve_entity_reference(directory, kind="character", reference="小周", selected_entity_id="char_zhou_ning")
    assert selected["entity_id"] == "char_zhou_ning"
    with pytest.raises(ChapterFactIdentityError, match="selection_invalid"):
        resolve_entity_reference(directory, kind="character", reference="小周", selected_entity_id="item_beacon")
    with pytest.raises(ChapterFactIdentityError, match="identity_unknown"):
        resolve_entity_reference(directory, kind="character", reference="周队长", selected_entity_id="char_zhou_lan")


def test_author_nickname_binding_is_local_and_does_not_claim_a_new_global_alias(directory):
    decision = {"local_key":"captain", "kind":"character", "action":"reuse", "review_source":"author",
        "reference":"周队长", "entity_id":"char_zhou_lan", "reason":"本章周队长明确指周岚"}
    result = plan(directory, [decision])
    assert result["local_references"]["captain"] == {"kind":"character", "entity_id":"char_zhou_lan"}
    assert result["directory_after"] == normalize_entity_directory(directory)
    assert result["declarations"] == []
    assert result["decisions"] == [decision]
    assert result["publication_authorized"] is False


def test_author_reuse_must_not_redirect_a_known_name_to_another_entity(directory):
    decision = {"local_key":"guard", "kind":"character", "action":"reuse", "review_source":"author",
        "reference":"周岚", "entity_id":"char_zhou_ning", "reason":"错误选择"}
    with pytest.raises(ChapterFactIdentityError, match="selection_invalid"):
        plan(directory, [decision])
    decision["reference"] = "小周"
    result = plan(directory, [decision])
    assert result["local_references"]["guard"]["entity_id"] == "char_zhou_ning"


def test_new_character_location_item_have_stable_ids_without_fake_quantity_or_position(directory):
    decisions = [declare(aliases=["小赵"]), declare("new_place", "location", "北门"),
                 declare("new_item", "item", "铜钥匙")]
    original = copy.deepcopy(directory)
    first = plan(directory, decisions)
    assert first == plan(directory, list(reversed(decisions)))
    for declaration in first["declarations"]:
        assert set(declaration) == {"entity_id", "kind", "name", "aliases"}
        resolved = resolve_entity_reference(first["directory_after"], kind=declaration["kind"], reference=declaration["name"])
        assert resolved["entity_id"] == declaration["entity_id"]
    assert resolve_entity_reference(first["directory_after"], kind="character", reference="小赵")["entity_id"] == first["local_references"]["new_guard"]["entity_id"]
    assert directory == original
    assert first["local_references"] != plan(directory, decisions, book_id="another-book")["local_references"]
    renamed = copy.deepcopy(decisions)
    renamed[0]["name"] = "赵远"
    second = plan(directory, renamed)
    assert first["local_references"] == second["local_references"]
    assert first["plan_sha256"] != second["plan_sha256"]


@pytest.mark.parametrize("decisions", [
    [declare(name="周岚")], [declare(aliases=["小周"])],
    [declare("one"), declare("two")],
])
def test_new_identity_cannot_silently_duplicate_existing_names_or_aliases(directory, decisions):
    before = copy.deepcopy(directory)
    with pytest.raises(ChapterFactIdentityError, match="reuse_required"):
        plan(directory, decisions)
    assert directory == before


@pytest.mark.parametrize("change", [
    {"review_source":"policy"}, {"reason":""}, {"action":"replace"},
    {"quantity":0}, {"entity_id":"client-chosen-id"}, {"name":""},
    {"kind":"relationship"}, {"kind":[]}, {"action":[]},
    {"aliases":"alias"}, {"local_key":"bad key"},
])
def test_identity_decisions_reject_forged_source_state_and_invalid_shapes(directory, change):
    with pytest.raises(ChapterFactIdentityError):
        plan(directory, [{**declare(), **change}])


def test_directory_shape_does_not_accept_quantities_as_identity_or_duplicate_ids(directory):
    with pytest.raises(ChapterFactIdentityError):
        normalize_entity_directory([{**directory[0], "quantity":0}])
    with pytest.raises(ChapterFactIdentityError):
        normalize_entity_directory([directory[0], directory[0]])
    with pytest.raises(ChapterFactIdentityError):
        plan(directory, [declare(), declare()])


def test_real_reviewed_fact_context_directory_can_supply_identity_decisions(ready):
    actual = build_chapter_fact_context(ready["trusted"], ready["text"]).to_dict()["entity_directory"]
    original = copy.deepcopy(actual)
    result = plan(actual, [declare()], book_id=ready["project"].book_id)
    assert resolve_entity_reference(result["directory_after"], kind="character", reference="林澈")["entity_id"] == "char_lin_che"
    assert resolve_entity_reference(result["directory_after"], kind="item", reference="item_beacon", reference_type="id")["entity_id"] == "item_beacon"
    assert actual == original
    assert result["publication_authorized"] is False
    # This component does not pretend to fill a location or stock baseline,
    # stage a transaction, or advance the real sample's chapter cursor.
    assert infer_next_chapter(ready["root"]) == 2
