def update_snapshot(snapshot, analysis):
    snapshot["chapter_index"] += 1

    # 简化示例
    snapshot["timeline"].append(analysis)

    return snapshot
