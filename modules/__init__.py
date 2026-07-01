"""Feature modules used by the execution engine."""

from modules.chapter_generator import generate_chapter
from modules.claude_polish import polish_chapter
from modules.conflict_engine import analyze_chapter
from modules.scene_repair import apply_repair_plan, build_repair_plan, repair_scene

__all__ = [
    "analyze_chapter",
    "apply_repair_plan",
    "build_repair_plan",
    "generate_chapter",
    "polish_chapter",
    "repair_scene",
]
