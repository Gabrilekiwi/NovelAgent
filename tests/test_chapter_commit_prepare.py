from __future__ import annotations

import copy
from dataclasses import asdict
import hashlib

import pytest

from core.engine.chapter_candidate_context import ChapterCandidateContextBuilder
from core.engine.chapter_commit_prepare import (
    ChapterCommitPreparationError, normalize_commit_prose, prepare_reviewed_chapter,
    require_same_idempotent_request, verify_preparation_unchanged,
)
from core.engine.chapter_fact_candidate import normalize_fact_proposal, normalize_fact_review_items, ChapterFactCandidateError
from core.engine.chapter_fact_contract import fact_contract_example
from core.memory_v2.canonical import canonical_json_hash
from core.story_project.read_set import StoryProjectSourceDriftError
from scripts.experiments.d1_reviewed_state import load_runtime_view
from scripts.experiments.run_d1_source_experiment import prepare_isolated_book
from scripts.experiments.run_d2_b0_compatibility import inventory
from workbench.chapter_facts import ChapterFactReviewRecord, _fact_confirmation_sha256


def hash_value(value):
    return canonical_json_hash(value, exclude_environment_fields=False)


def text_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def case():
    book = prepare_isolated_book()
    trusted = ChapterCandidateContextBuilder(book["root"], reviewed_state_loader=load_runtime_view).build(3)
    example = fact_contract_example()
    prefix = "第三章。\n"
    text = prefix + example["chapter_text"]
    proposal = copy.deepcopy(example["proposal"])
    proposal["facts"] = [f for f in proposal["facts"] if f["kind"] in {"event","counter"}]
    facts = normalize_fact_proposal(proposal, chapter_text=text)
    for fact in facts:
        fact["review"]["decision"] = "accepted"
    identity = trusted.story_project_context["project_identity"]
    read_set = trusted.story_project_context["read_set"]
    authority = {k:book["identity"].authority[k] for k in ("mode","authority_epoch","head_event_hash")}
    authority.update(identity_revision=read_set["identity_revision"],authority_projection_sha256=None)
    draft = {"id":"draft-test", "project_id":"project-test", "candidate_id":"candidate-test", "chapter":3,
             "text":text, "sha256":text_hash(text), "revision":1, "state":"editing"}
    # Real store record shape and confirmation algorithm; no fabricated author
    # activity is attributed to a user (all data here are synthetic fixtures).
    record = ChapterFactReviewRecord(record_type="chapter_fact_review",schema_version="1.0",
        id="review-test",project_id="project-test",chapter=3,candidate_id="candidate-test",
        candidate_intent_hash="a"*64,draft_id=draft["id"],source_draft_revision=1,
        source_draft_sha256=draft["sha256"],source_text=text,context_sha256=trusted.context_sha256,
        source_extraction_id=None,source_extraction_record_hash=None,authority=authority,
        authority_sha256=hash_value(authority),facts=facts,facts_sha256=hash_value(facts),revision=1,
        state="confirmed",confirmation_sha256=None,created_at="synthetic",updated_at="synthetic",confirmed_at="synthetic")
    review = asdict(record)
    review["confirmation_sha256"] = _fact_confirmation_sha256(record)
    return {"root":book["root"],"inputs":{"trusted_context":trusted,"project_id":"project-test",
        "draft":draft,"review":review,"idempotency_key":"request-1"}}


def test_preparation_uses_actual_context_and_changes_state_without_writing(case):
    before = inventory(case["root"])
    inputs = case["inputs"]
    original = copy.deepcopy(inputs)
    plan = prepare_reviewed_chapter(**inputs)
    assert inventory(case["root"]) == before
    assert inputs == original
    assert plan["fact_preparation"]["state_after"]["numeric_counters"]["alert_level"]["current_value"] == 4
    assert plan["binding"]["committed_through"] == 2 and plan["binding"]["chapter"] == 3
    assert plan["publication_authorized"] is False
    assert plan["final_prose"] == inputs["draft"]["text"].rstrip()+"\n"
    assert verify_preparation_unchanged(plan,**inputs) == plan
    require_same_idempotent_request(plan,prepare_reviewed_chapter(**inputs))


@pytest.mark.parametrize("field,value", [("project_id","other"),("chapter",4),("draft_id","other"),
    ("source_draft_revision",2),("source_draft_sha256","f"*64),("context_sha256","f"*64),("candidate_id","other")])
def test_review_from_another_revision_or_context_is_rejected(case,field,value):
    case["inputs"]["review"][field] = value
    with pytest.raises(ChapterCommitPreparationError,match="review_stale"):
        prepare_reviewed_chapter(**case["inputs"])


@pytest.mark.parametrize("mutation", ["draft_revision","review_revision","fact_decision","candidate_intent"])
def test_old_preparation_and_same_key_reject_changed_inputs(case,mutation):
    inputs = case["inputs"]
    old = prepare_reviewed_chapter(**inputs)
    if mutation == "draft_revision":
        inputs["draft"]["revision"] += 2  # edited and then restored to identical text
        inputs["review"]["source_draft_revision"] += 2
    elif mutation == "review_revision":
        inputs["review"]["revision"] += 1
    elif mutation == "candidate_intent":
        inputs["review"]["candidate_intent_hash"] = "b"*64
    else:
        inputs["review"]["facts"][-1]["review"]["decision"] = "rejected"
        inputs["review"]["facts_sha256"] = hash_value(inputs["review"]["facts"])
    with pytest.raises(ChapterCommitPreparationError,match="preparation_stale"):
        verify_preparation_unchanged(old,**inputs)
    incoming = prepare_reviewed_chapter(**inputs)
    with pytest.raises(ChapterCommitPreparationError,match="idempotency_conflict"):
        require_same_idempotent_request(old,incoming)


def test_manual_source_edit_invalidates_prepare_and_keeps_original_files(case):
    source = case["root"] / "追踪/上下文.md"
    source.write_text(source.read_text(encoding="utf-8")+"\n作者补充新的情况。",encoding="utf-8")
    before = inventory(case["root"])
    with pytest.raises(StoryProjectSourceDriftError):
        prepare_reviewed_chapter(**case["inputs"])
    assert inventory(case["root"]) == before


def test_occupied_chapter_rejects_old_read_set(case):
    (case["root"] / "正文/第003章_另稿.md").write_text("已占用",encoding="utf-8")
    with pytest.raises(ChapterCommitPreparationError,match="sequence_changed"):
        prepare_reviewed_chapter(**case["inputs"])


@pytest.mark.parametrize("field,value",[("state","editing"),("review_source","automatic_policy"),("record_type","policy_acceptance")])
def test_automatic_acceptance_cannot_become_author_confirmation(case,field,value):
    case["inputs"]["review"][field] = value
    with pytest.raises(ChapterCommitPreparationError,match="review_unconfirmed"):
        prepare_reviewed_chapter(**case["inputs"])


def test_authority_mutation_with_recomputed_hash_is_rejected(case):
    review = case["inputs"]["review"]
    review["authority"]["authority_epoch"] = 5
    review["authority_sha256"] = hash_value(review["authority"])
    with pytest.raises(ChapterCommitPreparationError,match="authority_invalid"):
        prepare_reviewed_chapter(**case["inputs"])


def test_corrupt_prepared_after_state_is_not_trusted(case):
    plan = prepare_reviewed_chapter(**case["inputs"])
    plan["fact_preparation"]["state_after"]["numeric_counters"]["alert_level"]["current_value"] = 99
    with pytest.raises(ChapterCommitPreparationError,match="preparation_stale"):
        verify_preparation_unchanged(plan,**case["inputs"])


def test_evidence_is_rechecked_against_final_normalized_prose(case):
    inputs = case["inputs"]
    text = inputs["draft"]["text"].rstrip()+"   "
    inputs["draft"].update(text=text,sha256=text_hash(text))
    inputs["review"].update(source_text=text,source_draft_sha256=text_hash(text))
    # A quote spanning trailing whitespace is valid in the draft but not the
    # exact bytes that the existing writer will publish. Do not move offsets.
    inputs["review"]["facts"][0]["evidence_spans"] = [{"start":len(text)-4,"end":len(text),"quote":text[-4:]}]
    inputs["review"]["facts_sha256"] = hash_value(inputs["review"]["facts"])
    assert normalize_fact_review_items(inputs["review"]["facts"],chapter_text=text)
    with pytest.raises(ChapterFactCandidateError,match="does not match the bound chapter text"):
        prepare_reviewed_chapter(**inputs)


def test_normalization_matches_writer_without_altering_inner_whitespace():
    assert normalize_commit_prose("  开头\r\n正文  \r\n\t") == "  开头\r\n正文\n"


def test_missing_idempotency_hashes_are_not_equal_requests():
    with pytest.raises(ChapterCommitPreparationError,match="binding_invalid"):
        require_same_idempotent_request({}, {})
