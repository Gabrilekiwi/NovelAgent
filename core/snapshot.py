def load_snapshot(path="data/snapshot.json"):
    import json
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_snapshot(snapshot, path="data/snapshot.json"):
    import json
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)