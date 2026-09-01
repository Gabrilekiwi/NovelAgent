from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.contracts import ModelResponse  # noqa: E402
from core.engine.locked_chapter import recover_locked_chapter  # noqa: E402
from core.engine.locked_chapter_state import (  # noqa: E402
    active_locked_chapter_checkpoint,
)
from core.engine.persistence import atomic_create_json, atomic_write_text  # noqa: E402
from core.engine.run_record import (  # noqa: E402
    build_failed_run_record,
    validate_run_result,
)
from core.execution_provenance import (  # noqa: E402
    build_execution_provenance,
    validate_execution_provenance,
)
from core.model_calls import ModelCallStore, build_scene_generation_call_id  # noqa: E402
from core.schema import validate_schema  # noqa: E402
from core.state.snapshot import load_snapshot, save_snapshot  # noqa: E402
from core.story_project.identity import (  # noqa: E402
    PROJECT_IDENTITY_SCHEMA_VERSION,
    ProjectIdentity,
    load_project_identity,
    validate_project_identity,
)
from core.story_project.model import CORE_DIRECTORY_NAMES  # noqa: E402
from core.story_project.paths import (  # noqa: E402
    canonical_outline_path,
    canonical_prose_path,
    infer_next_chapter,
    resolve_outline,
    resolve_prose,
    scan_prose_chapters,
)


DEFAULT_TARGET = Path("books") / "_validation_chapter20_recovery"
CHAPTER_INDEX = 20
EXPECTED_SCENE_COUNT = 3
REUSABLE_SCENE_COUNT = 2
EXECUTION_ID = "execution_c20_fixture"


class FixturePreparationError(RuntimeError):
    """The synthetic recovery fixture could not be prepared safely."""


class SyntheticPartialResponseInterruption(RuntimeError):
    """Stable failure type recorded in the synthetic RunRecord."""


def prepare_fixture(
    target: str | Path,
    *,
    allowed_root: str | Path = ROOT,
    now: datetime | None = None,
    book_id: str | None = None,
) -> dict[str, Any]:
    """Create a new, isolated Chapter 20 recovery fixture.

    The target must not exist and must stay inside ``allowed_root``.  The
    function never calls a model, never updates the Workbench database, and
    never overwrites an existing StoryProject.
    """

    allowed = Path(allowed_root).resolve(strict=True)
    destination = _resolve_new_target(target, allowed_root=allowed)
    created_at = _as_utc(now or datetime.now(timezone.utc))
    fixture_book_id = str(book_id or uuid.uuid4())
    if not fixture_book_id.strip():
        raise FixturePreparationError("book_id must be non-empty")

    with _inherited_temporary_directory(
        destination.parent,
        prefix=".c20-fixture-",
    ) as staging:
        try:
            _populate_fixture(
                staging,
                final_root=destination,
                book_id=fixture_book_id,
                created_at=created_at,
            )
            inspection = _inspect_unclassified_fixture(
                staging,
                expected_book_id=fixture_book_id,
            )
            probe = _probe_recovery_classification(
                staging,
                expected_book_id=fixture_book_id,
                now=created_at + timedelta(minutes=2),
            )
            if destination.exists():
                raise FixturePreparationError(
                    f"target appeared while the fixture was being prepared: {destination}"
                )
            os.rename(staging, destination)
        except Exception as exc:
            if isinstance(exc, FixturePreparationError):
                raise
            raise FixturePreparationError(f"could not prepare fixture: {exc}") from exc

    return {
        "status": "ready",
        "synthetic": True,
        "purpose": "chapter20_workbench_recovery_vertical_slice",
        "target": str(destination),
        "book_id": fixture_book_id,
        "chapter_index": CHAPTER_INDEX,
        "formal_chapter_count": inspection["formal_chapter_count"],
        "expected_scene_count": probe["expected_scene_count"],
        "expected_reusable_scene_count": probe["reusable_scene_count"],
        "expected_next_scene_index": probe["next_scene_index"],
        "unresolved_attempt_count": inspection["unresolved_attempt_count"],
        "provider_calls": 0,
        "workbench_import_required": True,
        "fixture_manifest": str(
            destination / ".novelagent" / "validation_fixture.json"
        ),
    }


def _resolve_new_target(target: str | Path, *, allowed_root: Path) -> Path:
    raw = Path(target).expanduser()
    candidate = raw if raw.is_absolute() else allowed_root / raw
    destination = candidate.resolve(strict=False)
    try:
        destination.relative_to(allowed_root)
    except ValueError as exc:
        raise FixturePreparationError(
            f"target must stay inside the repository: {destination}"
        ) from exc
    if destination == allowed_root:
        raise FixturePreparationError("the repository root cannot be a fixture target")
    if destination.exists():
        raise FixturePreparationError(
            f"target already exists; refusing to overwrite it: {destination}"
        )
    try:
        parent = destination.parent.resolve(strict=True)
    except OSError as exc:
        raise FixturePreparationError(
            f"target parent must already exist: {destination.parent}"
        ) from exc
    if not parent.is_dir():
        raise FixturePreparationError(f"target parent is not a directory: {parent}")
    try:
        parent.relative_to(allowed_root)
    except ValueError as exc:
        raise FixturePreparationError(
            f"target parent must stay inside the repository: {parent}"
        ) from exc
    return destination


@contextmanager
def _inherited_temporary_directory(
    parent: Path,
    *,
    prefix: str,
) -> Iterator[Path]:
    """Create a private-by-name work directory that inherits the parent ACL.

    On this project's Windows/Python 3.12 environment, ``tempfile.mkdtemp`` can
    create a mode-0700 ACL that the creating process itself cannot traverse.
    ``Path.mkdir`` with its normal mode inherits the repository ACL instead.
    """

    resolved_parent = parent.resolve(strict=True)
    if not resolved_parent.is_dir():
        raise FixturePreparationError(
            f"temporary directory parent is not a directory: {resolved_parent}"
        )
    temporary: Path | None = None
    for _ in range(10):
        candidate = resolved_parent / f"{prefix}{uuid.uuid4().hex[:8]}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        temporary = candidate
        break
    if temporary is None:
        raise FixturePreparationError(
            f"could not allocate a unique temporary directory in {resolved_parent}"
        )

    block_error: BaseException | None = None
    try:
        try:
            yield temporary
        except BaseException as exc:
            block_error = exc
            raise
    finally:
        if os.path.lexists(temporary):
            try:
                shutil.rmtree(temporary)
            except OSError as exc:
                message = (
                    "could not remove owned temporary directory "
                    f"{temporary}: {exc}"
                )
                if block_error is not None:
                    block_error.add_note(message)
                else:
                    raise FixturePreparationError(message) from exc


def _populate_fixture(
    root: Path,
    *,
    final_root: Path,
    book_id: str,
    created_at: datetime,
) -> None:
    for directory in CORE_DIRECTORY_NAMES:
        (root / directory).mkdir(exist_ok=False)
    (root / ".novelagent").mkdir(exist_ok=False)

    identity = ProjectIdentity(
        schema_version=PROJECT_IDENTITY_SCHEMA_VERSION,
        book_id=book_id,
        created_at=created_at.isoformat(),
        root_hint=str(final_root),
        story_state_mode="shadow",
        activation=None,
        ephemeral=False,
    )
    validate_project_identity(identity.to_dict())
    atomic_create_json(root / ".novelagent" / "project.json", identity.to_dict())

    _write_story_project_sources(root)
    snapshot_path = root / ".novelagent" / "runtime" / "snapshot.json"
    save_snapshot(
        {
            "chapter_index": CHAPTER_INDEX,
            "project_profile": {
                "title": "第20章恢复验收（合成夹具）",
                "genre": "本地恢复验证",
                "language": "zh-CN",
                "known_characters": ["林舟"],
                "known_locations": ["地下站"],
            },
            "story_state": {
                "last_chapter_ending": "林舟抵达停电的地下站入口。",
                "last_scene_location": "地下站入口",
                "last_scene_characters": ["林舟"],
                "open_threads": ["隧道深处的求救信号来源不明"],
                "required_opening_bridge": "从地下站入口的停电现场直接继续。",
            },
            # The recovery snapshot says the next scene resumes at the station
            # entrance.  Register that exact location in the canonical world
            # state as well, so the real bridge pre-validation used by the
            # Workbench can verify the continuation before it reaches a model
            # call.
            "world_state": {
                "locations": {
                    "地下站入口": {
                        "status": "停电",
                    },
                },
            },
        },
        snapshot_path,
    )

    run_dir = root / ".novelagent" / "runtime" / "runs"
    run_dir.mkdir(parents=True, exist_ok=False)
    provenance = _write_execution_provenance(run_dir)
    _write_model_call_evidence(run_dir, created_at=created_at)
    run_id = _write_failed_run(
        run_dir,
        story_root=final_root,
        book_id=book_id,
        started_at=created_at,
        provenance=provenance,
    )

    manifest = {
        "schema_version": "1.0",
        "synthetic": True,
        "purpose": "chapter20_workbench_recovery_vertical_slice",
        "created_at": created_at.isoformat(),
        "book_id": book_id,
        "chapter_index": CHAPTER_INDEX,
        "source_run_id": run_id,
        "execution_id": EXECUTION_ID,
        "formal_chapters": list(range(1, CHAPTER_INDEX)),
        "expected_scene_count": EXPECTED_SCENE_COUNT,
        "successful_scene_indexes": [1, 2],
        "partial_unsettled_scene_index": 3,
        "provider_calls": 0,
        "limitations": [
            "synthetic evidence is for recovery UX and state-flow validation only",
            "this fixture is not real Provider smoke or literary-quality evidence",
        ],
    }
    atomic_create_json(root / ".novelagent" / "validation_fixture.json", manifest)
    atomic_write_text(
        root / "README.md",
        "# 第 20 章恢复验收夹具\n\n"
        "这是 NovelAgent 自动生成的合成隔离项目。它模拟场景 1、2 已结算、"
        "场景 3 仅留下部分响应且没有 Receipt 的中断。\n\n"
        "本项目只用于工作台恢复流程验收，不是正式小说、真实 Provider smoke "
        "或文学质量证据。请从工作台书架导入本目录，不要复制其中证据到正式书。\n",
    )


def _write_story_project_sources(root: Path) -> None:
    atomic_write_text(
        root / "设定" / "恢复验收设定.md",
        "# 恢复验收设定\n\n"
        "主角林舟正在一座断电的地下站调查求救信号。地下站只有备用灯可用，"
        "正式正文必须通过 NovelAgent 的事务链提交。\n",
    )
    atomic_write_text(
        root / "追踪" / "恢复验收追踪.md",
        "# 恢复验收追踪\n\n"
        "- 林舟：位于地下站入口。\n"
        "- 未解线索：隧道深处的第二次求救信号。\n",
    )
    atomic_write_text(
        root / "大纲" / "总纲.md",
        "# 合成恢复验收总纲\n\n"
        "林舟进入断电地下站，确认信号来源并建立可安全撤离的路径。\n",
    )
    outline = (
        "# 第 20 章：停电站台\n\n"
        "核心事件：林舟进入断电地下站，确认求救信号的真实来源。\n\n"
        "## 必写节拍\n\n"
        "- 林舟在地下站入口核对断电记录并确认只有备用灯仍亮\n"
        "- 林舟带队越过检票闸机并封闭身后的维修门\n"
        "- 林舟抵达站台并定位隧道深处的求救信号\n\n"
        "结尾压力：隧道深处响起第二次求救信号。\n"
    )
    atomic_write_text(canonical_outline_path(root, CHAPTER_INDEX), outline)

    for chapter in range(1, CHAPTER_INDEX):
        text = (
            f"# 第 {chapter} 章：验收前史\n\n"
            f"这是隔离恢复项目的第 {chapter} 章合成前史。林舟沿着既定路线推进，"
            "记录沿途的门禁、照明和撤离标记；这些内容只为让工作台稳定推断下一章，"
            "不代表正式小说质量，也不应复制到生产书籍。\n\n"
            "章末，林舟继续接近地下站，并把所有未确认信号留在追踪清单中。\n"
        )
        atomic_write_text(
            canonical_prose_path(root, chapter, "验收前史"),
            text,
        )


def _write_model_call_evidence(run_dir: Path, *, created_at: datetime) -> None:
    store = ModelCallStore(
        run_dir / "executions" / EXECUTION_ID / "model_calls"
    )
    for scene_index in (1, 2):
        attempt_id = f"attempt-scene-{scene_index:02d}"
        timestamp = created_at + timedelta(seconds=scene_index)
        response_text = _scene_response(scene_index)
        store.create_intent(
            call_id=build_scene_generation_call_id(scene_index),
            attempt_id=attempt_id,
            provider="synthetic_fixture",
            model="no-provider-call",
            stage="chapter_generation",
            budget_reservation={
                "reserved_input_tokens": 0,
                "reserved_output_tokens": 0,
            },
            request={"fixture": True, "scene_index": scene_index},
            created_at=timestamp,
        )
        relative = f"responses/{attempt_id}.json"
        atomic_write_text(store.root / relative, response_text)
        store.create_receipt(
            attempt_id,
            response=ModelResponse(
                response_text,
                usage={"input_tokens": 0, "output_tokens": 0},
                finish_reason="stop",
                actual_model="synthetic-fixture",
                endpoint_type="compatible",
            ),
            response_artifact_ref=relative,
            status="succeeded",
            received_at=timestamp,
        )

    scene_index = 3
    attempt_id = "attempt-scene-03"
    store.create_intent(
        call_id=build_scene_generation_call_id(scene_index),
        attempt_id=attempt_id,
        provider="synthetic_fixture",
        model="no-provider-call",
        stage="chapter_generation",
        budget_reservation={
            "reserved_input_tokens": 0,
            "reserved_output_tokens": 0,
        },
        request={"fixture": True, "scene_index": scene_index},
        created_at=created_at + timedelta(seconds=scene_index),
    )
    atomic_write_text(
        store.root / "responses" / f"{attempt_id}.partial.txt",
        "林舟刚踏上站台，隧道深处的第二次求救信号突然——",
    )


def _write_execution_provenance(run_dir: Path) -> dict[str, str]:
    provenance = build_execution_provenance(
        code_bundle_hash=hashlib.sha256(
            b"novelagent-synthetic-chapter20-recovery-fixture"
        ).hexdigest(),
        code_file_count=0,
        git_commit=None,
        git_dirty=False,
        prompt_hashes={},
        schema_hashes={},
        dependency_versions={},
        provider="synthetic_fixture",
        model="no-provider-call",
        config={"purpose": "chapter20_recovery_vertical_slice"},
        feature_flags={"synthetic_fixture": True},
    )
    payload = validate_schema(
        provenance.to_dict(),
        "execution_provenance.schema.json",
    )
    relative = f"executions/{EXECUTION_ID}/provenance.json"
    atomic_create_json(run_dir / Path(relative), payload)
    return {
        "provenance_hash": provenance.provenance_hash,
        "provenance_artifact_ref": relative,
    }


def _scene_response(scene_index: int) -> str:
    if scene_index == 1:
        beat = "林舟在地下站入口核对断电记录并确认只有备用灯仍亮"
        prose = (
            f"{beat}。他逐项比对墙上的断路器编号、值班表与应急灯电量，"
            "先把仍能使用的灯位标在纸图上，再让同行者沿墙根保持间距。"
            "入口外的风把卷帘门吹得轻响，他没有急着深入，而是复核退路、"
            "无线电频道和集合手势，直到每个人都能在黑暗里重复撤离顺序。"
        )
    elif scene_index == 2:
        beat = "林舟带队越过检票闸机并封闭身后的维修门"
        prose = (
            f"{beat}。他先用手电扫过闸机后的积水，再依次引导队员踩过干燥地砖，"
            "确认最后一人通过后才合上维修门。门锁落下时，站厅里的回声被截断，"
            "林舟重新清点装备和人数，把入口到站台的每一个转角记入撤离路线，"
            "随后示意所有人压低声音，准备沿停止运行的扶梯继续向下。"
        )
    else:
        raise FixturePreparationError(f"unsupported synthetic scene: {scene_index}")
    payload = {
        "prose": prose,
        "events": [
            {
                "event_id": f"chapter-{CHAPTER_INDEX:04d}-beat-{scene_index:03d}",
                "type": f"required_beat_{scene_index}_completed",
                "subjects": [],
                "objects": [],
                "location": "",
                "status": "completed",
            }
        ],
        "deltas": {
            "characters": [],
            "relationships": [],
            "rosters": [],
            "locations": [],
            "inventory": [],
            "counters": [],
        },
        "continuity_note": f"合成验收场景 {scene_index} 已完成。",
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _write_failed_run(
    run_dir: Path,
    *,
    story_root: Path,
    book_id: str,
    started_at: datetime,
    provenance: dict[str, str],
) -> str:
    decision = {
        "chapter_index": CHAPTER_INDEX,
        "goal": "continue_existing_arc",
        "actions": ["generate_chapter", "validate"],
        "validation_focus": ["logic", "story_project"],
        "max_repair_attempts": 1,
        "notes": ["synthetic recovery fixture"],
    }
    metadata = {
        "kind": "chapter_input_pack",
        "chapter_index": CHAPTER_INDEX,
        "chars": 7,
        "sections": ["chapter_index"],
        "decision": {
            "goal": decision["goal"],
            "actions": decision["actions"],
            "validation_focus": decision["validation_focus"],
            "max_repair_attempts": decision["max_repair_attempts"],
        },
        "snapshot": {
            "world_state_keys": [],
            "character_count": 0,
            "timeline_count": 0,
            "constraint_count": 0,
            "memory_source": "synthetic_fixture",
            "memory_status": "ready",
            "memory_item_count": 0,
        },
        "memory_index": {
            "source": "synthetic_fixture",
            "status": "ready",
            "item_count": 0,
            "indexed_item_count": 0,
            "source_mapping_count": 0,
            "last_run_present": False,
        },
        "recovery_context": {
            "available": False,
            "source_run_id": None,
            "status": None,
            "problem_count": 0,
            "executed_checks": [],
            "skipped_checks": [],
            "repair_attempts": 0,
        },
        "story_project": {
            "enabled": True,
            "chapter_index": CHAPTER_INDEX,
            "required_beat_count": EXPECTED_SCENE_COUNT,
            "planned_scene_count": EXPECTED_SCENE_COUNT,
            "ending_pressure_present": True,
        },
    }
    error = SyntheticPartialResponseInterruption(
        "synthetic scene 3 response ended before a durable receipt was written"
    )
    record = build_failed_run_record(
        started_at=started_at,
        finished_at=started_at + timedelta(minutes=1),
        base_snapshot={"chapter_index": CHAPTER_INDEX},
        runtime_snapshot={"chapter_index": CHAPTER_INDEX},
        memory_context={
            "source": "synthetic_fixture",
            "status": "ready",
            "items": [],
        },
        decision=decision,
        workflow=["generate_chapter", "validate"],
        input_pack="第 20 章",
        input_pack_metadata=metadata,
        chapter="",
        validation=None,
        repair_attempts=0,
        workflow_trace=[],
        error=error,
    )
    record["story_project"] = {
        "enabled": True,
        "mode": "apply",
        "root": str(story_root),
        "book_id": book_id,
        "chapter_index": CHAPTER_INDEX,
        "chapter_blueprint": None,
        "source_paths": None,
        "source_resolution": None,
        "writeback": {
            "attempted": False,
            "applied": False,
            "partial": False,
            "dry_run": False,
            "overwrite": False,
            "targets": [],
            "blocked_reasons": [],
            "errors": [],
            "failed_targets": [],
            "diff_summary": {},
            "artifacts": {},
        },
    }
    record["execution_evidence"] = {
        "execution_id": EXECUTION_ID,
        "provenance_hash": provenance["provenance_hash"],
        "provenance_artifact_ref": provenance["provenance_artifact_ref"],
        "model_calls_ref": f"executions/{EXECUTION_ID}/model_calls",
    }
    payload = {"run": record, "chapter": ""}
    validate_run_result(payload)
    run_path = run_dir / f"{record['id']}.json"
    atomic_write_text(
        run_path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )
    return str(record["id"])


def _inspect_unclassified_fixture(
    root: Path,
    *,
    expected_book_id: str,
) -> dict[str, int]:
    identity = load_project_identity(root)
    if identity is None or identity.book_id != expected_book_id:
        raise FixturePreparationError("fixture ProjectIdentity is missing or mismatched")
    chapters = scan_prose_chapters(root)
    if set(chapters) != set(range(1, CHAPTER_INDEX)):
        raise FixturePreparationError("fixture must contain exactly formal chapters 1-19")
    if infer_next_chapter(root) != CHAPTER_INDEX:
        raise FixturePreparationError("fixture next chapter is not 20")
    if resolve_prose(root, CHAPTER_INDEX).path is not None:
        raise FixturePreparationError("fixture unexpectedly contains formal chapter 20")
    outline = resolve_outline(root, CHAPTER_INDEX)
    if outline.conflict or outline.path is None:
        raise FixturePreparationError("fixture must contain one unique chapter 20 outline")

    snapshot_path = root / ".novelagent" / "runtime" / "snapshot.json"
    if int(load_snapshot(snapshot_path)["chapter_index"]) != CHAPTER_INDEX:
        raise FixturePreparationError("fixture snapshot is not positioned at chapter 20")
    run_dir = root / ".novelagent" / "runtime" / "runs"
    run_paths = list(run_dir.glob("chapter_20_*.json"))
    if len(run_paths) != 1:
        raise FixturePreparationError("fixture must contain exactly one chapter 20 run")
    payload = json.loads(run_paths[0].read_text(encoding="utf-8"))
    validate_run_result(payload)
    run = payload["run"]
    if (
        run.get("status") != "failed"
        or run.get("accepted") is not False
        or run.get("committed") is not False
        or int(run.get("chapter_index") or 0) != CHAPTER_INDEX
    ):
        raise FixturePreparationError("fixture RunRecord is not a failed chapter 20 run")
    evidence = run.get("execution_evidence")
    expected_provenance_ref = f"executions/{EXECUTION_ID}/provenance.json"
    expected_model_calls_ref = f"executions/{EXECUTION_ID}/model_calls"
    if (
        not isinstance(evidence, dict)
        or evidence.get("execution_id") != EXECUTION_ID
        or evidence.get("provenance_artifact_ref") != expected_provenance_ref
        or evidence.get("model_calls_ref") != expected_model_calls_ref
    ):
        raise FixturePreparationError("fixture execution evidence is not cross-bound")
    provenance = validate_execution_provenance(
        json.loads(
            (run_dir / Path(expected_provenance_ref)).read_text(encoding="utf-8")
        )
    )
    if (
        provenance.get("provenance_hash") != evidence.get("provenance_hash")
        or provenance.get("model", {}).get("provider") != "synthetic_fixture"
    ):
        raise FixturePreparationError("fixture provenance is missing or mismatched")

    store = ModelCallStore(
        run_dir / "executions" / EXECUTION_ID / "model_calls"
    )
    for scene_index in (1, 2):
        receipt = store.load_receipt(f"attempt-scene-{scene_index:02d}")
        if receipt.get("status") != "succeeded":
            raise FixturePreparationError(
                f"fixture scene {scene_index} is missing its successful receipt"
            )
    unresolved = store.list_uncertain_calls()
    if [item.get("attempt_id") for item in unresolved] != ["attempt-scene-03"]:
        raise FixturePreparationError(
            "fixture must contain only the unsettled scene 3 attempt"
        )
    partial_path = store.root / "responses" / "attempt-scene-03.partial.txt"
    if not partial_path.is_file() or store.has_receipt("attempt-scene-03"):
        raise FixturePreparationError(
            "fixture scene 3 must have an untrusted partial artifact and no receipt"
        )
    return {
        "formal_chapter_count": len(chapters),
        "unresolved_attempt_count": len(unresolved),
    }


def _probe_recovery_classification(
    source: Path,
    *,
    expected_book_id: str,
    now: datetime,
) -> dict[str, Any]:
    with _inherited_temporary_directory(
        source.parent,
        prefix=".chapter20-recovery-probe-",
    ) as temporary:
        probe = temporary / "fixture"
        shutil.copytree(source, probe)
        run_dir = probe / ".novelagent" / "runtime" / "runs"
        snapshot_path = probe / ".novelagent" / "runtime" / "snapshot.json"
        result = recover_locked_chapter(
            story_project_root=probe,
            run_dir=run_dir,
            snapshot_path=snapshot_path,
            expected_book_id=expected_book_id,
            language="zh-CN",
            clock=lambda: now,
            id_factory=lambda: "fixtureprobe",
        )
        checkpoint = active_locked_chapter_checkpoint(
            run_dir,
            chapter_index=CHAPTER_INDEX,
            expected_book_id=expected_book_id,
            language="zh-CN",
        )
        if (
            result.get("ok") is not True
            or result.get("action") != "resume_scenes"
            or result.get("provider_calls") != 0
            or result.get("reusable_scene_count") != REUSABLE_SCENE_COUNT
            or result.get("expected_scene_count") != EXPECTED_SCENE_COUNT
            or result.get("next_scene_index") != 3
            or not isinstance(checkpoint, dict)
            or len(checkpoint.get("scenes") or []) != REUSABLE_SCENE_COUNT
        ):
            raise FixturePreparationError(
                "fixture recovery probe did not classify scenes 1-2 as reusable"
            )
        return result


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FixturePreparationError("fixture timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create a new synthetic StoryProject for the Chapter 20 Workbench "
            "recovery vertical slice. No model is called and existing paths are "
            "never overwritten."
        )
    )
    parser.add_argument(
        "--target",
        default=str(DEFAULT_TARGET),
        help=(
            "New directory inside this repository. Defaults to "
            "books/_validation_chapter20_recovery."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = prepare_fixture(args.target)
    except (FixturePreparationError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "code": "chapter20_recovery_fixture_not_created",
                    "message": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
