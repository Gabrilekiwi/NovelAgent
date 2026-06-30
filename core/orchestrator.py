from core.snapshot import load_snapshot, save_snapshot
from core.input_pack import build_input_pack
from core.generator import generate_chapter
from core.analyzer import analyze_chapter
from core.updater import update_snapshot


def run_once():
    snapshot = load_snapshot()

    input_pack = build_input_pack(snapshot)

    chapter = generate_chapter(input_pack)

    analysis = analyze_chapter(chapter)

    snapshot = update_snapshot(snapshot, analysis)

    save_snapshot(snapshot)

    return chapter
