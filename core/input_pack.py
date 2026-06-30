def build_input_pack(snapshot):
    return f"""
    你是小说生成器，请根据以下世界状态生成下一章：
    
    # Chapter Index: {snapshot['chapter_index']}
    
    # World State:
    {snapshot['world_state']}
    
    # Characters:
    {snapshot['characters']}
    
    # Timeline:
    {snapshot['timeline']}
    
    要求：
    - 推进剧情
    - 引入冲突
    - 保持一致性
    """
