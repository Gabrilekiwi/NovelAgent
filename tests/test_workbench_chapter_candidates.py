from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
from typing import Callable
import uuid

from fastapi.testclient import TestClient
import pytest

from api.contracts import MODEL_ENDPOINT_OFFICIAL
from core.engine.chapter_candidate import ChapterCandidateExecutor, ChapterCandidateStore
from core.execution_provenance import build_execution_provenance
from core.model_call_runtime import current_model_call_runtime
from core.runtime_paths import RuntimePaths
from core.story_project.activation import (
    activate_story_state,
    build_story_state_calibration_report,
)
from core.story_project.identity import ensure_project_identity
from core.story_project.semantic_contracts import STORY_PROJECT_SEMANTIC_STATE_SCHEMA_VERSION
from core.story_project.semantic_parser import SEMANTIC_PARSER_VERSION
from workbench.app import create_app


REPOSITORY = Path(__file__).resolve().parents[1]
FIXTURE = (
    REPOSITORY
    / "tests"
    / "fixtures"
    / "story_project_semantics"
    / "cases"
    / "synthetic_standard"
    / "book"
)
PROSE = "林澈贴着闸门辨认回声，周岚核对控制器记录。两人确认通道安全后，沿北侧入口进入控制室。"


def _qualified_report(book_id: str) -> dict:
    return build_story_state_calibration_report(
        book_id=book_id,
        parser_version=SEMANTIC_PARSER_VERSION,
        semantic_schema_version=STORY_PROJECT_SEMANTIC_STATE_SCHEMA_VERSION,
        target_layout_profile_version="canonical-zh-1",
        evidence={
            "target_sample_count": 1,
            "format_variant_count": 2,
            "managed_round_trip_rate": 1.0,
            "required_field_exact_match_rate": 1.0,
            "authoritative_precision": 1.0,
            "supported_optional_recall": 0.95,
            "unsupported_structure_count": 1,
            "unsupported_structure_captured_count": 1,
            "consecutive_shadow_chapters": 10,
            "blocking_conflict_count": 0,
            "missing_provenance_fields": [],
        },
        generated_at="2026-09-04T00:00:00+00:00",
    )


def _strict_book(tmp_path: Path) -> Path:
    # Candidate evidence adds several deliberately descriptive path segments.
    # Keep the fixture root short enough for non-long-path-enabled Windows CI.
    root = tmp_path / "b"
    shutil.copytree(FIXTURE, root)
    outline = root / "大纲" / "细纲_第002章.md"
    outline.write_text(
        outline.read_text(encoding="utf-8").replace(
            "结尾压力：",
            "结尾压力：危险迫使两人立刻做出选择；",
            1,
        ),
        encoding="utf-8",
    )
    identity = ensure_project_identity(root)
    activate_story_state(root, _qualified_report(identity.book_id))
    return root


def _fixed_provenance():
    return build_execution_provenance(
        code_bundle_hash="1" * 64,
        code_file_count=1,
        git_commit=None,
        git_dirty=True,
        prompt_hashes={"prompts/chapter_prompt.md": "2" * 64},
        schema_hashes={"schemas/chapter_candidate_record.schema.json": "3" * 64},
        dependency_versions={},
        provider="test",
        model="deterministic-test-model",
        config={"test_mode": True},
        feature_flags={"chapter_candidate": True, "formal_state_write": False},
    )


class DeterministicExecutorFactory:
    def __init__(self, behavior: Callable[[Path, str], str]) -> None:
        self.behavior = behavior
        self.generator_calls = 0
        self.inputs: list[str] = []

    def __call__(self, root: Path) -> ChapterCandidateExecutor:
        project_root = Path(root)

        def generate(input_pack: str) -> str:
            self.generator_calls += 1
            self.inputs.append(input_pack)
            return self.behavior(project_root, input_pack)

        return ChapterCandidateExecutor(
            story_project_root=project_root,
            generator=generate,
            execution_kind="deterministic",
            repository_root=REPOSITORY,
            provenance_factory=_fixed_provenance,
        )


class ProviderUncertainExecutorFactory:
    """Exercise durable Provider evidence without making any network request."""

    def __init__(self) -> None:
        self.mode = "uncertain"
        self.physical_calls = 0

    def __call__(self, root: Path) -> ChapterCandidateExecutor:
        project_root = Path(root)
        if self.mode == "completed":
            return ChapterCandidateExecutor(
                story_project_root=project_root,
                generator=lambda _input: PROSE,
                execution_kind="deterministic",
                repository_root=REPOSITORY,
                provenance_factory=_fixed_provenance,
            )

        def generate(input_pack: str) -> str:
            runtime = current_model_call_runtime()
            assert runtime is not None
            call_id = runtime.new_call_id(
                provider="test",
                stage="chapter_candidate_generation",
            )

            def unknown_result():
                self.physical_calls += 1
                raise TimeoutError("simulated unknown Provider outcome")

            return runtime.execute_attempt(
                call_id=call_id,
                attempt_number=1,
                provider="test",
                model="test-model",
                stage="chapter_candidate_generation",
                endpoint_type=MODEL_ENDPOINT_OFFICIAL,
                request={
                    "input_sha256": hashlib.sha256(
                        input_pack.encode("utf-8")
                    ).hexdigest()
                },
                input_tokens=12,
                max_output_tokens=128,
                operation=unknown_result,
            ).text

        return ChapterCandidateExecutor(
            story_project_root=project_root,
            generator=generate,
            execution_kind="model",
            repository_root=REPOSITORY,
            provenance_factory=_fixed_provenance,
        )


class _SimulatedProcessExit(BaseException):
    pass


def _create_app(
    *,
    tmp_path: Path,
    root: Path,
    factory: Callable[[Path], ChapterCandidateExecutor],
):
    return create_app(
        database_path=tmp_path / "workbench.db",
        runtime_root=tmp_path / "job-runtime",
        allowed_workspaces=[root.parent],
        repo_root=REPOSITORY,
        start_worker=False,
        auto_import_active=False,
        chapter_candidate_executor_factory=factory,
    )


def _import_project(app, root: Path) -> dict:
    return app.state.projects.import_project(
        root,
        name="候选正文测试书",
        metadata={"language": "zh-CN"},
    ).to_dict()


def _unlock(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200


def _confirm_chapter(client: TestClient, project_id: str) -> dict:
    card_response = client.get(f"/api/projects/{project_id}/chapters/2/card")
    assert card_response.status_code == 200, card_response.text
    card = card_response.json()["chapter_card"]
    response = client.post(
        f"/api/projects/{project_id}/chapters/2/card/confirm",
        headers={"origin": "http://testserver"},
        json={
            "confirmed": True,
            "expected_card_sha256": card["confirmation"]["card_sha256"],
            "goal": "进入封闭区并确认失踪信号来源",
            "validation_focus": ["continuity", "spatial", "logic"],
            "notes_text": "保持第三人称限知\n不要提前揭示信号源身份",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["confirmation"]


def _generate_candidate(
    client: TestClient,
    project_id: str,
    confirmation: dict,
    *,
    key: str,
):
    return client.post(
        f"/api/projects/{project_id}/chapters/2/candidates",
        headers={
            "origin": "http://testserver",
            "Idempotency-Key": key,
        },
        json={"confirmation": confirmation},
    )


def _formal_inventory(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative.startswith(".novelagent/runtime/"):
            continue
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _assert_no_private_candidate_fields(payload: object, root: Path) -> None:
    serialized = json.dumps(payload, ensure_ascii=False)
    assert str(root) not in serialized
    for forbidden in (
        "input_pack",
        "input.md",
        "source_artifact_ref",
        "artifact_ref",
        ".novelagent",
        "story_project_root",
        "head_event_hash",
        "provenance_artifact_ref",
        "model_calls_ref",
    ):
        assert forbidden not in serialized


def _assert_no_formal_runtime_outputs(root: Path) -> None:
    paths = RuntimePaths.for_story_project(root)
    assert not paths.snapshot_path.exists()
    for forbidden in (
        paths.chapter_dir,
        paths.review_dir,
        paths.persistence_dir,
        paths.delivery_dir,
        paths.memory_dir,
    ):
        assert not forbidden.exists(), str(forbidden)
    assert not any(paths.run_dir.glob("run_*.json"))
    assert not any(paths.run_dir.rglob("publication_receipt*.json"))
    assert not any(paths.run_dir.rglob("transaction*.json"))


def test_candidate_api_creates_an_isolated_editable_draft_with_reload_and_cas(
    tmp_path: Path,
) -> None:
    root = _strict_book(tmp_path)
    factory = DeterministicExecutorFactory(lambda _root, _input: PROSE)
    app = _create_app(tmp_path=tmp_path, root=root, factory=factory)
    project = _import_project(app, root)
    project_id = project["id"]
    formal_before = _formal_inventory(root)

    with TestClient(app) as client:
        _unlock(client)
        confirmation = _confirm_chapter(client, project_id)
        response = _generate_candidate(
            client,
            project_id,
            confirmation,
            key="candidate-api-normal-001",
        )

        assert response.status_code == 200, response.text
        candidate = response.json()["candidate"]
        assert candidate["status"] == "completed"
        assert candidate["raw_status"] == "completed"
        assert candidate["prose"] == {
            "available": True,
            "sha256": hashlib.sha256(PROSE.encode("utf-8")).hexdigest(),
            "chars": len(PROSE),
            "immutable": True,
            "text": PROSE,
        }
        assert candidate["draft_import"] == {"status": "ready"}
        draft = candidate["draft"]
        assert draft["candidate_id"] == candidate["candidate_id"]
        assert draft["project_id"] == project_id
        assert draft["chapter"] == 2
        assert draft["text"] == PROSE
        assert draft["sha256"] == candidate["prose"]["sha256"]
        assert draft["source_sha256"] == candidate["prose"]["sha256"]
        assert draft["revision"] == 1
        _assert_no_private_candidate_fields(response.json(), root)
        assert factory.generator_calls == 1
        assert len(factory.inputs) == 1
        assert "Do not output event ledgers" in factory.inputs[0]

        edited_text = PROSE + "\n\n林澈停下脚步，把尚未确认的判断留在心里。"
        edited_response = client.put(
            f"/api/candidate-drafts/{draft['id']}",
            headers={"origin": "http://testserver"},
            json={"text": edited_text, "expected_revision": 1},
        )
        assert edited_response.status_code == 200, edited_response.text
        edited = edited_response.json()["draft"]
        assert edited["text"] == edited_text
        assert edited["revision"] == 2
        assert edited["source_sha256"] == draft["source_sha256"]

        stale_edit = client.put(
            f"/api/candidate-drafts/{draft['id']}",
            headers={"origin": "http://testserver"},
            json={"text": "这次并发旧写入不得覆盖作者文本。", "expected_revision": 1},
        )
        assert stale_edit.status_code == 409, stale_edit.text
        assert stale_edit.json()["error"]["code"] == "chapter_candidate_conflict"

        repeated = _generate_candidate(
            client,
            project_id,
            confirmation,
            key="candidate-api-normal-001",
        )
        assert repeated.status_code == 200, repeated.text
        repeated_candidate = repeated.json()["candidate"]
        assert repeated_candidate["candidate_id"] == candidate["candidate_id"]
        assert repeated_candidate["draft"]["text"] == edited_text
        assert repeated_candidate["draft"]["revision"] == 2
        assert factory.generator_calls == 1

        listed_response = client.get(
            f"/api/projects/{project_id}/chapters/2/candidates"
        )
        assert listed_response.status_code == 200, listed_response.text
        listed = listed_response.json()["items"]
        assert [item["candidate_id"] for item in listed] == [candidate["candidate_id"]]
        assert "text" not in listed[0]["prose"]
        assert "text" not in listed[0]["draft"]
        assert listed[0]["draft"]["revision"] == 2
        _assert_no_private_candidate_fields(listed_response.json(), root)

        detail_response = client.get(
            (
                f"/api/projects/{project_id}/chapters/2/candidates/"
                f"{candidate['candidate_id']}"
            )
        )
        assert detail_response.status_code == 200, detail_response.text
        detail = detail_response.json()["candidate"]
        assert detail["prose"]["text"] == PROSE
        assert detail["draft"]["text"] == edited_text
        assert detail["draft"]["revision"] == 2
        _assert_no_private_candidate_fields(detail_response.json(), root)

        immutable = ChapterCandidateStore(root).load_prose(candidate["candidate_id"])
        assert immutable == PROSE
        with app.state.database.connection() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM chapter_candidate_drafts WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT COUNT(*) FROM drafts WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM cohorts").fetchone()[0] == 0

    reloaded_app = _create_app(tmp_path=tmp_path, root=root, factory=factory)
    with TestClient(reloaded_app) as reloaded_client:
        _unlock(reloaded_client)
        reloaded_response = reloaded_client.get(
            f"/api/projects/{project_id}/chapters/2/candidates"
        )
        assert reloaded_response.status_code == 200, reloaded_response.text
        reloaded = reloaded_response.json()["items"]
        assert len(reloaded) == 1
        assert reloaded[0]["candidate_id"] == candidate["candidate_id"]
        assert "text" not in reloaded[0]["prose"]
        assert "text" not in reloaded[0]["draft"]
        assert reloaded[0]["draft"]["revision"] == 2
        _assert_no_private_candidate_fields(reloaded_response.json(), root)

        reloaded_detail_response = reloaded_client.get(
            (
                f"/api/projects/{project_id}/chapters/2/candidates/"
                f"{candidate['candidate_id']}"
            )
        )
        assert reloaded_detail_response.status_code == 200, reloaded_detail_response.text
        reloaded_detail = reloaded_detail_response.json()["candidate"]
        assert reloaded_detail["prose"]["text"] == PROSE
        assert reloaded_detail["draft"]["text"] == edited_text
        assert reloaded_detail["draft"]["revision"] == 2
        _assert_no_private_candidate_fields(reloaded_detail_response.json(), root)

    assert _formal_inventory(root) == formal_before
    _assert_no_formal_runtime_outputs(root)


def test_stale_candidate_is_retained_but_cannot_create_an_editable_draft(
    tmp_path: Path,
) -> None:
    root = _strict_book(tmp_path)
    outline = root / "大纲" / "细纲_第002章.md"
    original_outline = outline.read_text(encoding="utf-8")
    formal_before = _formal_inventory(root)

    def edit_outline_during_generation(project_root: Path, _input: str) -> str:
        target = project_root / "大纲" / "细纲_第002章.md"
        target.write_text(
            original_outline + "\n作者在生成期间调整了本章意图。\n",
            encoding="utf-8",
        )
        return PROSE

    factory = DeterministicExecutorFactory(edit_outline_during_generation)
    app = _create_app(tmp_path=tmp_path, root=root, factory=factory)
    project = _import_project(app, root)

    with TestClient(app) as client:
        _unlock(client)
        confirmation = _confirm_chapter(client, project["id"])
        response = _generate_candidate(
            client,
            project["id"],
            confirmation,
            key="candidate-api-stale-001",
        )

        assert response.status_code == 200, response.text
        candidate = response.json()["candidate"]
        assert candidate["status"] == "stale"
        assert candidate["raw_status"] == "stale"
        assert candidate["prose"]["available"] is True
        assert candidate["prose"]["text"] == PROSE
        assert candidate["draft"] is None
        assert candidate["draft_import"] == {"status": "not_available"}
        assert candidate["error"]["code"]
        _assert_no_private_candidate_fields(response.json(), root)

        adopt = client.post(
            (
                f"/api/projects/{project['id']}/chapters/2/candidates/"
                f"{candidate['candidate_id']}/draft"
            ),
            headers={"origin": "http://testserver"},
            json={
                "author_confirmed": True,
                "expected_prose_sha256": candidate["prose"]["sha256"],
            },
        )
        assert adopt.status_code == 409, adopt.text
        assert adopt.json()["error"]["code"] == "chapter_candidate_conflict"

        listed = client.get(
            f"/api/projects/{project['id']}/chapters/2/candidates"
        )
        assert listed.status_code == 200, listed.text
        assert listed.json()["items"][0]["status"] == "stale"
        assert listed.json()["items"][0]["draft"] is None
        with app.state.database.connection() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM chapter_candidate_drafts"
            ).fetchone()[0] == 0

    formal_after = _formal_inventory(root)
    outline_key = outline.relative_to(root).as_posix()
    assert formal_after[outline_key] != formal_before[outline_key]
    assert {
        key: value for key, value in formal_after.items() if key != outline_key
    } == {
        key: value for key, value in formal_before.items() if key != outline_key
    }
    assert outline.read_text(encoding="utf-8") == (
        original_outline + "\n作者在生成期间调整了本章意图。\n"
    )
    _assert_no_formal_runtime_outputs(root)


def test_failed_candidate_is_retained_without_draft_or_formal_changes(
    tmp_path: Path,
) -> None:
    root = _strict_book(tmp_path)
    formal_before = _formal_inventory(root)

    def fail_generation(_root: Path, _input: str) -> str:
        raise RuntimeError("simulated deterministic Candidate failure")

    factory = DeterministicExecutorFactory(fail_generation)
    app = _create_app(tmp_path=tmp_path, root=root, factory=factory)
    project = _import_project(app, root)

    with TestClient(app) as client:
        _unlock(client)
        confirmation = _confirm_chapter(client, project["id"])
        response = _generate_candidate(
            client,
            project["id"],
            confirmation,
            key="candidate-api-failed-001",
        )

        assert response.status_code == 200, response.text
        candidate = response.json()["candidate"]
        assert candidate["status"] == "failed"
        assert candidate["raw_status"] == "failed"
        assert candidate["prose"] == {
            "available": False,
            "sha256": None,
            "chars": 0,
            "immutable": True,
            "text": None,
        }
        assert candidate["draft"] is None
        assert candidate["draft_import"] == {"status": "not_available"}
        assert candidate["error"] == {
            "code": "candidate_generation_failed",
            "message": "正文候选生成失败；正式正文和权威状态均未修改。",
        }
        _assert_no_private_candidate_fields(response.json(), root)
        assert factory.generator_calls == 1

        listed_response = client.get(
            f"/api/projects/{project['id']}/chapters/2/candidates"
        )
        assert listed_response.status_code == 200, listed_response.text
        listed = listed_response.json()["items"]
        assert len(listed) == 1
        assert listed[0]["candidate_id"] == candidate["candidate_id"]
        assert listed[0]["status"] == "failed"
        assert listed[0]["draft"] is None
        _assert_no_private_candidate_fields(listed_response.json(), root)
        with app.state.database.connection() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM chapter_candidate_drafts"
            ).fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM cohorts").fetchone()[0] == 0

    assert _formal_inventory(root) == formal_before
    _assert_no_formal_runtime_outputs(root)


def test_source_drift_after_confirmation_is_rejected_before_candidate_or_generator(
    tmp_path: Path,
) -> None:
    root = _strict_book(tmp_path)
    factory = DeterministicExecutorFactory(lambda _root, _input: PROSE)
    app = _create_app(tmp_path=tmp_path, root=root, factory=factory)
    project = _import_project(app, root)
    outline = root / "大纲" / "细纲_第002章.md"

    with TestClient(app) as client:
        _unlock(client)
        confirmation = _confirm_chapter(client, project["id"])
        outline.write_text(
            outline.read_text(encoding="utf-8") + "\n作者在确认后修改了章纲。\n",
            encoding="utf-8",
        )
        after_author_edit = _formal_inventory(root)

        response = _generate_candidate(
            client,
            project["id"],
            confirmation,
            key="candidate-api-preflight-drift-001",
        )

        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "chapter_card_changed"
        assert factory.generator_calls == 0
        assert after_author_edit == _formal_inventory(root)
        candidate_root = RuntimePaths.for_story_project(root).runtime_dir / "candidates"
        assert not candidate_root.exists()
        with app.state.database.connection() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM chapter_candidate_drafts"
            ).fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM cohorts").fetchone()[0] == 0

    _assert_no_formal_runtime_outputs(root)


def test_intent_only_candidate_requires_exact_author_confirmed_resume_after_restart(
    tmp_path: Path,
) -> None:
    root = _strict_book(tmp_path)
    formal_before = _formal_inventory(root)

    def exit_after_claim(_root: Path, _input: str) -> str:
        raise _SimulatedProcessExit()

    interrupted_factory = DeterministicExecutorFactory(exit_after_claim)
    interrupted_app = _create_app(
        tmp_path=tmp_path,
        root=root,
        factory=interrupted_factory,
    )
    project = _import_project(interrupted_app, root)
    project_id = project["id"]

    with TestClient(interrupted_app) as client:
        _unlock(client)
        confirmation = _confirm_chapter(client, project_id)

    # A BaseException models a process exit and intentionally bypasses the
    # executor's normal terminal-record publication.  Invoke that crash at the
    # core seam so it cannot tear down TestClient's AnyIO portal; all recovery
    # assertions below still exercise the public Workbench API after restart.
    with pytest.raises(_SimulatedProcessExit):
        interrupted_factory(root).execute(
            chapter_index=2,
            confirmation=confirmation,
            idempotency_key="candidate-api-interrupted-001",
        )

    candidate_root = RuntimePaths.for_story_project(root).runtime_dir / "candidates"
    candidates = [path.name for path in candidate_root.iterdir() if path.is_dir()]
    assert len(candidates) == 1
    candidate_id = candidates[0]
    candidate_store = ChapterCandidateStore(root)
    intent = candidate_store.load_intent(candidate_id)
    assert not candidate_store.has_record(candidate_id)
    assert interrupted_factory.generator_calls == 1

    recovery_factory = DeterministicExecutorFactory(lambda _root, _input: PROSE)
    reloaded_app = _create_app(tmp_path=tmp_path, root=root, factory=recovery_factory)
    with TestClient(reloaded_app) as client:
        _unlock(client)
        listed_response = client.get(
            f"/api/projects/{project_id}/chapters/2/candidates"
        )
        assert listed_response.status_code == 200, listed_response.text
        listed = listed_response.json()["items"]
        assert len(listed) == 1
        pending = listed[0]
        assert pending["candidate_id"] == candidate_id
        assert pending["status"] == "needs_recovery"
        assert pending["raw_status"] == "needs_recovery"
        assert pending["record_hash"] is None
        assert pending["draft"] is None
        assert pending["error"] == {
            "code": "candidate_needs_recovery",
            "message": "候选已有持久 Intent，但没有终态记录；系统不会自动恢复或重试。",
        }
        assert "text" not in pending["prose"]
        _assert_no_private_candidate_fields(listed_response.json(), root)

        without_confirmation = client.post(
            (
                f"/api/projects/{project_id}/chapters/2/candidates/"
                f"{candidate_id}/resume"
            ),
            headers={"origin": "http://testserver"},
            json={
                "expected_intent_hash": intent["intent_hash"],
                "author_confirmed": False,
            },
        )
        assert without_confirmation.status_code == 409, without_confirmation.text
        assert recovery_factory.generator_calls == 0

        wrong_hash = client.post(
            (
                f"/api/projects/{project_id}/chapters/2/candidates/"
                f"{candidate_id}/resume"
            ),
            headers={"origin": "http://testserver"},
            json={
                "expected_intent_hash": "0" * 64,
                "author_confirmed": True,
            },
        )
        assert wrong_hash.status_code == 409, wrong_hash.text
        assert wrong_hash.json()["error"]["code"] == "chapter_candidate_conflict"
        assert recovery_factory.generator_calls == 0

        unknown = client.get(
            f"/api/projects/{project_id}/chapters/2/candidates/candidate_{'0' * 40}"
        )
        assert unknown.status_code == 404, unknown.text
        assert unknown.json()["error"]["code"] == "chapter_candidate_not_found"

        wrong_chapter = client.get(
            f"/api/projects/{project_id}/chapters/3/candidates/{candidate_id}"
        )
        assert wrong_chapter.status_code == 409, wrong_chapter.text
        assert wrong_chapter.json()["error"]["code"] == "chapter_candidate_conflict"

        resumed_response = client.post(
            (
                f"/api/projects/{project_id}/chapters/2/candidates/"
                f"{candidate_id}/resume"
            ),
            headers={"origin": "http://testserver"},
            json={
                "expected_intent_hash": intent["intent_hash"],
                "author_confirmed": True,
            },
        )
        assert resumed_response.status_code == 200, resumed_response.text
        resumed = resumed_response.json()["candidate"]
        assert resumed["candidate_id"] == candidate_id
        assert resumed["status"] == "completed"
        assert resumed["prose"]["text"] == PROSE
        assert resumed["draft"]["text"] == PROSE
        assert resumed["draft"]["revision"] == 1
        assert recovery_factory.generator_calls == 1
        _assert_no_private_candidate_fields(resumed_response.json(), root)

    assert _formal_inventory(root) == formal_before
    _assert_no_formal_runtime_outputs(root)


def test_provider_uncertain_abandon_gate(
    request: pytest.FixtureRequest,
) -> None:
    short_tmp = REPOSITORY / ".tmp" / "pu" / uuid.uuid4().hex[:8]
    short_tmp.mkdir(parents=True)
    request.addfinalizer(lambda: shutil.rmtree(short_tmp, ignore_errors=True))
    root = _strict_book(short_tmp)
    formal_before = _formal_inventory(root)
    factory = ProviderUncertainExecutorFactory()
    app = _create_app(tmp_path=short_tmp, root=root, factory=factory)
    project = _import_project(app, root)
    project_id = project["id"]

    with TestClient(app) as client:
        _unlock(client)
        confirmation = _confirm_chapter(client, project_id)
        uncertain_response = _generate_candidate(
            client,
            project_id,
            confirmation,
            key="candidate-api-provider-uncertain-001",
        )

        assert uncertain_response.status_code == 200, uncertain_response.text
        uncertain = uncertain_response.json()["candidate"]
        raw_uncertain_record = ChapterCandidateStore(root).load_record(
            uncertain["candidate_id"]
        )
        assert uncertain["status"] == "provider_uncertain", (
            uncertain["error"],
            uncertain["uncertainty"],
            factory.physical_calls,
            raw_uncertain_record["error"],
        )
        assert "可能已计费" in uncertain["error"]["message"]
        assert uncertain["raw_status"] == "provider_uncertain"
        assert uncertain["draft"] is None
        assert uncertain["uncertainty"]["count"] == 1
        assert uncertain["uncertainty"]["abandoned"] is False
        assert len(uncertain["uncertainty"]["sha256"]) == 64
        assert factory.physical_calls == 1
        _assert_no_private_candidate_fields(uncertain_response.json(), root)

        frozen = _generate_candidate(
            client,
            project_id,
            confirmation,
            key="candidate-api-provider-must-remain-frozen",
        )
        assert frozen.status_code == 409, frozen.text
        assert frozen.json()["error"]["code"] == "chapter_candidate_conflict"
        assert factory.physical_calls == 1

        store = ChapterCandidateStore(root)
        record_bytes = store.record_path(uncertain["candidate_id"]).read_bytes()
        wrong_abandon = client.post(
            (
                f"/api/projects/{project_id}/chapters/2/candidates/"
                f"{uncertain['candidate_id']}/abandon-provider"
            ),
            headers={"origin": "http://testserver"},
            json={
                "expected_intent_hash": uncertain["intent_hash"],
                "expected_record_hash": uncertain["record_hash"],
                "expected_uncertainty_sha256": "0" * 64,
                "author_confirmed": True,
            },
        )
        assert wrong_abandon.status_code == 409, wrong_abandon.text
        assert factory.physical_calls == 1

        abandoned_response = client.post(
            (
                f"/api/projects/{project_id}/chapters/2/candidates/"
                f"{uncertain['candidate_id']}/abandon-provider"
            ),
            headers={"origin": "http://testserver"},
            json={
                "expected_intent_hash": uncertain["intent_hash"],
                "expected_record_hash": uncertain["record_hash"],
                "expected_uncertainty_sha256": uncertain["uncertainty"]["sha256"],
                "author_confirmed": True,
            },
        )
        assert abandoned_response.status_code == 200, abandoned_response.text
        abandoned = abandoned_response.json()["candidate"]
        assert abandoned["status"] == "abandoned"
        assert abandoned["raw_status"] == "provider_uncertain"
        assert abandoned["uncertainty"]["abandoned"] is True
        assert store.record_path(uncertain["candidate_id"]).read_bytes() == record_bytes
        assert factory.physical_calls == 1

        factory.mode = "completed"
        completed_response = _generate_candidate(
            client,
            project_id,
            confirmation,
            key="candidate-api-after-provider-abandon-001",
        )
        assert completed_response.status_code == 200, completed_response.text
        completed = completed_response.json()["candidate"]
        assert completed["status"] == "completed"
        assert completed["candidate_id"] != uncertain["candidate_id"]
        assert completed["draft"]["text"] == PROSE
        assert factory.physical_calls == 1
        _assert_no_private_candidate_fields(completed_response.json(), root)

        with app.state.database.connection() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM chapter_candidate_drafts"
            ).fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM cohorts").fetchone()[0] == 0

    assert _formal_inventory(root) == formal_before
    _assert_no_formal_runtime_outputs(root)
