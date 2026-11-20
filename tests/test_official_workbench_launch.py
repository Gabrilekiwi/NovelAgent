from __future__ import annotations

from pathlib import Path
import os
import subprocess

import pytest

from workbench.official_launch import (
    OfficialLaunchError,
    launch_from_official_plan,
    load_source_credentials,
    official_worktree_path,
    plan_official_launch,
    prepare_official_worktree,
    workbench_sources_differ_from_head,
)


HEAD_INDEX = "<html><body>HEAD committed UI</body></html>\n"
DIRTY_INDEX = (
    "<html><body>DIRTY money-budget 自动连续写作 reviewed-batches</body></html>\n"
)
SECRET = "super-secret-test-token-not-for-logs"


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout or "git failed")
    return result


def _init_repo(root: Path) -> Path:
    repo = root / "NovelAgent"
    static = repo / "workbench" / "static"
    static.mkdir(parents=True)
    (static / "index.html").write_text(HEAD_INDEX, encoding="utf-8")
    (repo / "run_workbench.py").write_text(
        "print('serve-root', __file__)\n",
        encoding="utf-8",
    )
    (repo / ".active-book").write_text("books/OfficialV2", encoding="utf-8")
    (repo / "books" / "OfficialV2").mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "official-launch-test@example.com")
    _git(repo, "config", "user.name", "Official Launch Test")
    _git(repo, "add", "workbench/static/index.html", "run_workbench.py")
    _git(repo, "commit", "-m", "head ui")
    return repo


def _dirty_workbench(repo: Path) -> None:
    (repo / "workbench" / "static" / "index.html").write_text(DIRTY_INDEX, encoding="utf-8")
    (repo / "workbench" / "static" / "reviewed-batches.html").write_text(
        "<html>untracked dirty overlay</html>\n",
        encoding="utf-8",
    )


def test_official_worktree_path_defaults_to_sibling(tmp_path: Path) -> None:
    repo = tmp_path / "NovelAgent"
    repo.mkdir()
    assert official_worktree_path(repo) == (tmp_path / "NovelAgent-official-head")
    override = tmp_path / "custom-head"
    assert official_worktree_path(
        repo, environ={"NOVELAGENT_OFFICIAL_WORKTREE": str(override)}
    ) == override


def test_extracted_package_needs_no_git_even_inside_another_checkout(tmp_path, monkeypatch):
    outer = _init_repo(tmp_path)
    package = outer / "解压的候选包"
    package.mkdir()
    monkeypatch.setenv("PATH", "")
    assert not plan_official_launch(package, [], environ={}).redirected
    with pytest.raises(OfficialLaunchError, match="Git"):
        plan_official_launch(outer, [], environ={})


def test_clean_checkout_stays_in_place(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    plan = plan_official_launch(repo, ["--no-browser"], environ={})
    assert plan.redirected is False
    assert plan.serve_root == repo.resolve()
    assert plan.reason == "sources-match-head"
    assert workbench_sources_differ_from_head(repo) is False


def test_dirty_workbench_plan_redirects_to_head_worktree(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _dirty_workbench(repo)
    assert workbench_sources_differ_from_head(repo) is True
    plan = plan_official_launch(repo, ["--no-browser"], environ={})
    assert plan.redirected is True
    assert plan.serve_root == official_worktree_path(repo)
    assert plan.static_index == plan.serve_root / "workbench" / "static" / "index.html"
    assert plan.argv == ("--no-browser", "--workspace", str(repo.resolve()))
    # Do not prepare yet: the dirty source must still contain the overlay.
    assert "自动连续写作" in (repo / "workbench" / "static" / "index.html").read_text(
        encoding="utf-8"
    )


def test_use_working_tree_keeps_dirty_sources_for_explicit_preview(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _dirty_workbench(repo)
    plan = plan_official_launch(
        repo, ["--no-browser"], use_working_tree=True, environ={}
    )
    assert plan.redirected is False
    assert plan.serve_root == repo.resolve()
    env_plan = plan_official_launch(
        repo,
        ["--no-browser"],
        environ={"NOVELAGENT_USE_WORKING_TREE": "1"},
    )
    assert env_plan.redirected is False


def test_prepare_official_worktree_serves_head_and_leaves_source_dirty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    repo = _init_repo(tmp_path)
    _dirty_workbench(repo)
    (repo / ".env").write_text(f"OPENAI_API_KEY={SECRET}\n", encoding="utf-8")
    plan = plan_official_launch(repo, ["--no-browser"], environ={})
    serve = prepare_official_worktree(repo, plan.serve_root)
    assert serve == plan.serve_root.resolve()
    served = (serve / "workbench" / "static" / "index.html").read_text(encoding="utf-8")
    dirty = (repo / "workbench" / "static" / "index.html").read_text(encoding="utf-8")
    assert served == HEAD_INDEX
    assert "自动连续写作" not in served
    assert "DIRTY money-budget" not in served
    assert "reviewed-batches" not in served
    assert dirty == DIRTY_INDEX
    assert (repo / "workbench" / "static" / "reviewed-batches.html").is_file()
    assert not (serve / "workbench" / "static" / "reviewed-batches.html").exists()
    marker = (serve / ".active-book").read_text(encoding="utf-8").strip()
    assert Path(marker) == (repo / "books" / "OfficialV2").resolve()
    # The launch plan and helper output must not mention credential values.
    assert SECRET not in str(plan)
    assert SECRET not in repr(plan)
    loaded = load_source_credentials(repo)
    assert loaded is True
    assert os.environ.get("OPENAI_API_KEY") == SECRET


def test_prepare_failure_does_not_fall_back_to_dirty_tree(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _dirty_workbench(repo)
    blocker = official_worktree_path(repo)
    blocker.mkdir()
    (blocker / "not-a-git-repo.txt").write_text("blocked", encoding="utf-8")
    with pytest.raises(OfficialLaunchError, match="不会改用未提交工作区界面"):
        prepare_official_worktree(repo, blocker)
    assert (repo / "workbench" / "static" / "index.html").read_text(
        encoding="utf-8"
    ) == DIRTY_INDEX


def test_existing_parent_or_foreign_repository_is_never_checked_out(tmp_path):
    repo = _init_repo(tmp_path)
    other_root = tmp_path / "another"
    other_root.mkdir()
    other = _init_repo(other_root)
    nested = other / "ordinary-folder"
    nested.mkdir()
    before = _git(other, "symbolic-ref", "HEAD").stdout
    for destination in (repo, other, nested):
        with pytest.raises(OfficialLaunchError, match="不会改用未提交工作区界面"):
            prepare_official_worktree(repo, destination)
    assert _git(other, "symbolic-ref", "HEAD").stdout == before


def test_launch_from_official_plan_runs_head_worktree_script(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _init_repo(tmp_path)
    _dirty_workbench(repo)
    (repo / ".env").write_text(f"OPENAI_API_KEY={SECRET}\n", encoding="utf-8")
    plan = plan_official_launch(repo, ["--no-browser"], environ={})
    code = launch_from_official_plan(plan)
    captured = capsys.readouterr()
    assert code == 0
    assert "已提交的 HEAD 界面" in captured.out
    assert str(plan.serve_root.resolve()) in captured.out
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    # Child stdout may bypass capsys on Windows; the prepared tree is the proof.
    served = (plan.serve_root / "workbench" / "static" / "index.html").read_text(encoding="utf-8")
    assert served == HEAD_INDEX
    assert (plan.serve_root / "run_workbench.py").is_file()
    if os.environ.get("OPENAI_API_KEY") == SECRET:
        os.environ.pop("OPENAI_API_KEY", None)


def test_current_official_repo_plan_would_not_serve_dirty_static() -> None:
    repo = Path(__file__).resolve().parents[1]
    plan = plan_official_launch(repo, ["--no-browser"], environ={})
    # This production checkout currently carries uncommitted Codex workbench
    # sources, so official start must point at the sibling HEAD worktree.
    if not workbench_sources_differ_from_head(repo):
        pytest.skip("working tree sources already match HEAD")
    assert plan.redirected is True
    assert plan.serve_root == official_worktree_path(repo)
    assert plan.static_index.is_file()
    served = plan.static_index.read_text(encoding="utf-8")
    dirty = (repo / "workbench" / "static" / "index.html").read_text(encoding="utf-8")
    assert "为本章冻结价格并启用金额限额" not in served
    assert "自动连续写作" not in served
    assert "正在核对当前模型的金额配置条件。" not in served
    assert "当前可设置调用、token 与时间限制。金额控制还需核验接口计费依据，此处不提供金额止损承诺。" in served
    assert "自动连续写作" in dirty or "为本章冻结价格并启用金额限额" in dirty
    # This test only inspects the planned serve root; it must not checkout or
    # restart the live official workbench.
