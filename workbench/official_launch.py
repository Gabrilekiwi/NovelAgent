from __future__ import annotations

"""Launch the author workbench from a committed HEAD worktree when needed.

The Windows starter and run_workbench.py may be invoked from a checkout that
still has uncommitted workbench/core sources. Those overlays must not be served
as the official author UI. This helper plans and prepares a sibling git
worktree at HEAD, keeps the original workspace and .env in place, and never
prints credential values.
"""

from dataclasses import dataclass
from pathlib import Path
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence


SOURCE_PATHS: tuple[str, ...] = (
    "workbench",
    "core",
    "schemas",
    "api",
    "run_workbench.py",
)
OFFICIAL_LAUNCH_ENV = "NOVELAGENT_OFFICIAL_LAUNCH"
USE_WORKING_TREE_ENV = "NOVELAGENT_USE_WORKING_TREE"
WORKTREE_PATH_ENV = "NOVELAGENT_OFFICIAL_WORKTREE"
ACTIVE_BOOK_NAME = ".active-book"


class OfficialLaunchError(RuntimeError):
    """Official HEAD worktree could not be prepared; do not fall back to dirty sources."""


@dataclass(frozen=True)
class OfficialLaunchPlan:
    source_repo: Path
    serve_root: Path
    argv: tuple[str, ...]
    redirected: bool
    reason: str

    @property
    def static_index(self) -> Path:
        return self.serve_root / "workbench" / "static" / "index.html"


def official_worktree_path(repo: Path, *, environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    override = str(env.get(WORKTREE_PATH_ENV) or "").strip()
    if override:
        return Path(override).expanduser().absolute()
    root = repo.resolve()
    return root.parent / f"{root.name}-official-head"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _head_sha(repo: Path) -> str:
    result = _git(repo, "rev-parse", "HEAD")
    sha = (result.stdout or "").strip()
    if result.returncode != 0 or not sha:
        raise OfficialLaunchError("无法读取仓库 HEAD，不能改用已提交工作台界面。")
    return sha


def workbench_sources_differ_from_head(repo: Path) -> bool:
    """Return True when process-visible sources differ from committed HEAD."""
    # An extracted distribution is not a checkout, even inside another repo.
    # Only a checkout (including linked worktrees) needs Git at launch.
    if not os.path.lexists(repo / ".git"):
        return False
    try:
        result = _git(repo, "status", "--porcelain", "--", *SOURCE_PATHS)
    except OSError as exc:
        raise OfficialLaunchError("开发检出目录需要 Git 来核对启动版本，请安装 Git 或使用解压后的候选包。") from exc
    if result.returncode != 0:
        raise OfficialLaunchError("无法核对开发检出目录的 Git 状态，未启动工作台。")
    return bool((result.stdout or "").strip())


def resolve_active_book_path(repo: Path) -> Path | None:
    marker = repo / ACTIVE_BOOK_NAME
    if not marker.is_file():
        return None
    try:
        value = marker.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return None
    if not value:
        return None
    candidate = Path(value)
    path = candidate if candidate.is_absolute() else repo / candidate
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return path


def sync_active_book(source_repo: Path, worktree: Path) -> Path | None:
    """Copy the source .active-book into the worktree as an absolute path."""
    resolved = resolve_active_book_path(source_repo)
    if resolved is None:
        return None
    marker = worktree / ACTIVE_BOOK_NAME
    marker.write_text(str(resolved), encoding="utf-8")
    return resolved


def load_source_credentials(source_repo: Path) -> bool:
    """Load the source checkout .env into the current process. Never print values."""
    path = source_repo / ".env"
    if not path.is_file():
        return False
    from dotenv import load_dotenv
    return bool(load_dotenv(path, override=False))


def _has_option(argv: Sequence[str], option: str) -> bool:
    prefix = option + "="
    return any(item == option or item.startswith(prefix) for item in argv)


def _child_argv(source_repo: Path, argv: Sequence[str]) -> tuple[str, ...]:
    cleaned = [item for item in argv if item != "--use-working-tree"]
    if not _has_option(cleaned, "--workspace"):
        cleaned.extend(["--workspace", str(source_repo.resolve())])
    return tuple(cleaned)


def plan_official_launch(
    source_repo: Path,
    argv: Sequence[str] | None = None,
    *,
    use_working_tree: bool = False,
    environ: Mapping[str, str] | None = None,
) -> OfficialLaunchPlan:
    """Decide whether official start must serve committed HEAD from a worktree."""
    repo = source_repo.resolve()
    args = tuple(argv or ())
    env = os.environ if environ is None else environ
    worktree = official_worktree_path(repo, environ=env)
    in_place = OfficialLaunchPlan(
        source_repo=repo,
        serve_root=repo,
        argv=args,
        redirected=False,
        reason="working tree",
    )
    if use_working_tree or str(env.get(USE_WORKING_TREE_ENV) or "").strip():
        return OfficialLaunchPlan(**{**in_place.__dict__, "reason": "use-working-tree"})
    if str(env.get(OFFICIAL_LAUNCH_ENV) or "").strip():
        return OfficialLaunchPlan(**{**in_place.__dict__, "reason": "already-redirected"})
    if repo == worktree.resolve():
        return OfficialLaunchPlan(**{**in_place.__dict__, "reason": "already-official-worktree"})
    if not workbench_sources_differ_from_head(repo):
        return OfficialLaunchPlan(**{**in_place.__dict__, "reason": "sources-match-head"})
    return OfficialLaunchPlan(
        source_repo=repo,
        serve_root=worktree,
        argv=_child_argv(repo, args),
        redirected=True,
        reason="uncommitted-workbench-sources",
    )


def prepare_official_worktree(source_repo: Path, worktree: Path | None = None) -> Path:
    """Create or update the official HEAD worktree. Never modify the source dirty tree."""
    repo = source_repo.resolve()
    dest = (worktree or official_worktree_path(repo)).resolve()
    sha = _head_sha(repo)
    if dest.exists():
        current = _git(dest, "rev-parse", "HEAD")
        current_sha = (current.stdout or "").strip()
        if current.returncode != 0:
            raise OfficialLaunchError(f"官方界面目录存在但不是可用的 git 工作树：{dest}")
        if current_sha != sha:
            updated = _git(dest, "checkout", "--detach", sha)
            if updated.returncode != 0:
                detail = (updated.stderr or updated.stdout or "").strip() or "checkout failed"
                raise OfficialLaunchError(
                    "无法将官方界面工作树更新到当前 HEAD，不会改用未提交工作区界面。 "
                    + detail
                )
    else:
        created = subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "--detach", str(dest), sha],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if created.returncode != 0:
            detail = (created.stderr or created.stdout or "").strip() or "worktree add failed"
            raise OfficialLaunchError(
                "无法创建用于已提交界面的官方工作树，不会改用未提交工作区界面。 "
                + detail
            )
    sync_active_book(repo, dest)
    return dest


def launch_from_official_plan(plan: OfficialLaunchPlan, *, executable: str | None = None) -> int:
    """Prepare the HEAD worktree and run its workbench. Fail closed on prepare errors."""
    if not plan.redirected:
        raise OfficialLaunchError("launch_from_official_plan requires a redirected plan")
    serve = prepare_official_worktree(plan.source_repo, plan.serve_root)
    load_source_credentials(plan.source_repo)
    script = serve / "run_workbench.py"
    if not script.is_file():
        raise OfficialLaunchError(f"官方工作树缺少 run_workbench.py：{serve}")
    env = os.environ.copy()
    env[OFFICIAL_LAUNCH_ENV] = "1"
    print("工作台将使用已提交的 HEAD 界面启动，避免未提交工作区改动进入作者界面。", flush=True)
    print(f"官方界面目录：{serve}", flush=True)
    completed = subprocess.run(
        [executable or sys.executable, "-B", str(script), *plan.argv],
        cwd=str(serve),
        env=env,
    )
    return int(completed.returncode)
