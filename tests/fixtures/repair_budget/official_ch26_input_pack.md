# Input Pack: Chapter 26

- Run: `chapter_26_20260902T050842557412Z`
- Status: `failed`
- Committed: `False`

---

You are NovelAgent's chapter generation module. Generate the next chapter from the runtime Snapshot and Director decision.

# Chapter Index
26

# Project Profile
{
  "language": "zh-CN",
  "known_characters": [
    "陆沉",
    "苏晴",
    "赵铁",
    "陆遥",
    "林夏",
    "韩野",
    "顾岚",
    "R-17铅盒",
    "四名获救者",
    "火种一号",
    "三辆联锁车",
    "character-bc336fab9f77fde2",
    "仓管员"
  ],
  "known_locations": [
    "临江市地下商超",
    "监控室",
    "冷库",
    "地下停车场",
    "地下一层营业区",
    "负二层冷库",
    "消防站独立隔离柜",
    "单独两小时观察区",
    "消防站一层车库区",
    "北站地下轨道检修层下方",
    "消防站器材口外负载分流防线",
    "物流园外围维修道"
  ]
}

# Director Decision
{
  "chapter_index": 26,
  "goal": "continue_existing_arc",
  "actions": [
    "build_snapshot",
    "pre_validate_bridge",
    "generate_chapter",
    "polish",
    "validate",
    "repair_if_needed",
    "commit_snapshot"
  ],
  "validation_focus": [
    "continuity",
    "spatial",
    "logic"
  ],
  "max_repair_attempts": 1,
  "notes": [
    "Use snapshot as the runtime source of truth.",
    "Keep Notion memory read-only during this execution step.",
    "Memory items available: 19.",
    "Active constraints: 4."
  ]
}

# World State
{
  "settings": {
    "《尸潮之下：我能提取进化词条》": {
      "fields": {},
      "facts": [
        "类型：丧尸末日、生存、人性抉择、能力升级、基地经营、热血爽文",
        "目标气质：危机压迫感与高密度反击并行，升级有明确反馈，胜利始终伴随代价",
        "核心卖点：主角击杀变异体后能看见并提取“进化词条”，还能将词条赋予装备、设施或可信任的同伴",
        "长线悬念：所谓词条并非系统奖励，而是感染源筛选“新物种”的实验记录",
        "资源或同伴遭遇明确危机。",
        "主角用信息差与能力组合完成越级猎杀。",
        "获得可见、可组合的词条奖励。",
        "奖励立刻转化为战力、设施或团队优势。",
        "新能力暴露更大世界与更强敌人，并带来侵蚀或信任代价。",
        "前期“活下来”：个人战斗、物资搜集、救人与取舍，建立地下商超安全屋。",
        "中期“立规矩”：扩建地铁枢纽基地，争夺水源、电力、药品与人口，和不同幸存者势力博弈。",
        "后期“夺回世界”：解析赤潮源头，建设人类进化序列，对抗母巢与试图以末日重塑阶级的幕后组织。"
      ]
    },
    "临江市地下商超": {
      "fields": {},
      "facts": [
        "地下一层为营业区，地下二层为仓储区，西侧连通停车场，北侧有废弃防空通道。",
        "冷库拥有独立柴油发电机，但燃料只够连续运行十八小时。",
        "仓储区卷帘门可以隔绝普通丧尸，无法长期承受力量型变异体冲击。",
        "医药专柜与监控室分别位于营业区东南角和员工通道入口。",
        "监控室内门：连接监控室与员工通道内段，可正常开关。",
        "东侧外门：员工通道通往医药专柜方向的接应门，可由监控室控制。",
        "营业区侧门：感染者曾撞开的侧门，已被机械扣与倒塌货架封死，禁止再次通行。",
        "三扇门不是同一扇门；人物移动时必须明确门名。",
        "负二层仓储外廊：由员工通道内段经西南侧消防楼梯抵达，连接冷库外廊与地下停车场。",
        "地下停车场卸货区：位于停车场西侧，设有卸货卷帘门、柴油发电机本地控制柜、货车泊位和手动叉车。",
        "地面物流坡道：从地下停车场通往地面，坡道防火门与冷库外廊北侧相连。",
        "物流坡道东侧检修门：坡道中段通往负二层卸货区的机械人员门；门内有一段缓冲走廊和可手动落下的防火卷帘，可作为逐人检查的临时双门隔离区。",
        "卸货区维修坑：位于货车泊位北侧，平时覆盖钢板；移开钢板后可困住大型感染体，但承重边缘可能坍塌。",
        "检修门外防撞柱：紧贴东侧检修门外墙的两根钢柱，与北侧卸货区维修坑相隔一段车道；适合用钢索将贴门目标锁死在门侧，不能与维修坑混写成同一位置。",
        "入口位于负二层仓储外廊北墙，入口防火门已被幸存者从通道内侧锁死。",
        "通道向北延伸，连接一处废弃运输中转间、旧人防医院支线与北站外围维护网，不直接进入北站站厅。",
        "通道内部有独立低压供电、红色引导灯和R-17运输标记，说明赤潮前曾用于隐秘实验样本转运。",
        "人防医院支线防爆门：位于R-17运输中转间东南侧，门后依次为侧向缓冲廊、废弃人防医院地下病区；与运输线前后钢滑门分属不同方向。",
        "人防医院地下病区：拥有护士站、可封闭病房、隔离室、药房、设备控制间与通风检修梯，可作为临时安全点；R-17回收程序能远程牵引外侧门锁，但机械断电后可阻止自动开门。"
      ]
    },
    "进化词条体系": {
      "fields": {
        "应激爆发": "白",
        "腐味屏蔽": "蓝",
        "弱点视界": "蓝",
        "坚固": "白"
      },
      "facts": [
        "陆沉能看见感染体、特殊物品与极少数幸存者携带的词条。",
        "完成有效击杀或满足特殊条件后，才有概率提取词条。",
        "词条分为灰、白、蓝、紫、金、赤六阶，可升级、融合或转移。",
        "人体同时承载的词条有限；超载会增加侵蚀值，引发幻听、嗜血和人格剥离。",
        "将词条赋予装备或基地设施，不增加人体侵蚀，但需要更多材料与能量。",
        "提取必须由陆沉直接接触有效击杀后的目标或与目标相连的武器；向装备、门或设施转移时，陆沉必须同时维持“尸体—武器/钢索—自身—承载物”的连续接触链，且目标在三米内。接触链中断后本次词条不能远距离飞向设施。",
        "初始词条槽：2",
        "初始侵蚀值：0/100",
        "第一次提取必得“应激爆发”，但主角直到实战后才理解代价。",
        "侵蚀值超过30时出现感染者低语，超过60时产生攻击冲动，达到100将不可逆异化。"
      ]
    },
    "苏晴": {
      "fields": {
        "年龄": "30岁",
        "身份": "急诊科医生",
        "性格": "理性、克制、敢于质疑主角",
        "专长": "创伤救治、感染观察、有限资源分诊",
        "初始秘密": "曾参与赤潮病毒早期临床样本的接收工作",
        "与陆沉关系": "价值观盟友，也是阻止陆沉越过人性底线的人"
      },
      "facts": []
    },
    "赵铁": {
      "fields": {
        "年龄": "35岁",
        "身份": "冷链货车司机",
        "性格": "嘴碎、现实、讲义气，擅长缓解紧张气氛",
        "专长": "驾驶、维修、路线判断、物资估价",
        "初始立场": "只想带物资离开，不愿为陌生人冒险",
        "成长方向": "从精致利己者成长为基地后勤负责人"
      },
      "facts": []
    },
    "陆沉": {
      "fields": {
        "年龄": "27岁",
        "身份": "大型仓储商超夜班安全员，退役消防救援队员",
        "性格": "冷静、执行力强、不轻易许诺；一旦承诺便会死扛到底",
        "缺点": "习惯独自承担，把“保护”变成替别人做决定",
        "初始目标": "穿过封锁区，确认妹妹陆遥是否还活着",
        "长期目标": "建立普通人也能活下去的安全秩序",
        "底线": "不主动牺牲无辜者，不把同伴当消耗品",
        "核心矛盾": "为了保护人类不断获得非人的力量"
      },
      "facts": []
    }
  },
  "locations": {}
}

# Story State
{
  "last_chapter_ending": "三人没有追车，而是转身朝主控所在的楼顶准备推进。",
  "last_scene_location": "物流园外围维修道",
  "last_scene_characters": [
    "赵铁",
    "陆沉",
    "顾岚"
  ],
  "open_threads": [],
  "required_opening_bridge": "物流园外围维修道；赵铁、陆沉、顾岚"
}

# Spatial State
{
  "spaces": {
    "消防站一层车库区": {
      "source": "chapter_analysis"
    },
    "消防站独立隔离柜": {
      "source": "chapter_analysis"
    },
    "物流园外围维修道": {
      "source": "chapter_analysis"
    }
  },
  "connections": [],
  "character_positions": {
    "陆沉": "地下商超监控室"
  },
  "blocked_paths": [],
  "last_transition": {
    "source": "chapter_analysis",
    "to": "物流园外围维修道"
  }
}


# Authoritative State
{
  "schema_version": "1.0",
  "source_precedence": [
    "story_project_standard",
    "chapter_event",
    "model_inference"
  ],
  "characters": {
    "林夏": {
      "affiliation": "火种一号",
      "aliases": [
        "林夏"
      ],
      "canonical_name": "林夏",
      "character_id": "林夏",
      "identity": "火种一号成员",
      "role": "规则、记录与限时决策协调者",
      "source_tier": "story_project_standard",
      "last_observation": "林夏站在消防站一层车库区的分控柜前，按下泡沫管线开关。",
      "last_seen_chapter": 19
    },
    "苏晴": {
      "affiliation": "火种一号",
      "aliases": [
        "苏晴"
      ],
      "canonical_name": "苏晴",
      "character_id": "苏晴",
      "identity": "急诊科医生",
      "role": "创伤救治、感染观察与有限资源分诊负责人",
      "source_tier": "story_project_standard",
      "field": "public_disclosure",
      "before": null,
      "after": "依据风险公开条款主动公开曾接收并登记赤潮早期临床样本，明确不知道完整实验目的",
      "reason": "苏晴从传回的黑旗名册照片中认出早期临床样本编码前缀，并主动向营地说明自身职责边界",
      "scene_index": 2,
      "public_disclosure": "依据风险公开条款主动公开曾接收并登记赤潮早期临床样本，明确不知道完整实验目的"
    },
    "赵铁": {
      "aliases": [
        "赵铁"
      ],
      "canonical_name": "赵铁",
      "character_id": "赵铁",
      "last_observation": "\"\n\n赵铁蹲在冷库外临时接线箱前，半边身子钻进墙洞，手里的万用表指针抖得像要断掉。",
      "last_seen_chapter": 10,
      "source_tier": "model_inference"
    },
    "陆沉": {
      "after": "偏头痛明显加重，但没有新增人体词条，侵蚀值保持6/100",
      "aliases": [
        "陆沉",
        "character-bc336fab9f77fde2"
      ],
      "before": "偏头痛因再次使用弱点视界而短暂加剧，但未出现新的侵蚀表现",
      "canonical_name": "陆沉",
      "character_id": "陆沉",
      "condition": "健康",
      "current_goal": "封闭员工通道并寻找安全出口",
      "field": "physical_condition",
      "identity": "主角",
      "last_observation": "陆沉站在冷库门口，看着十七个人第一次被分成可以执行的四组。",
      "last_seen_chapter": 10,
      "physical_condition": "偏头痛明显加重，但没有新增人体词条，侵蚀值保持6/100",
      "reason": "陆沉在冷库内仅使用一次弱点视界定位八枚应答片的共用触发节点",
      "role": "主角",
      "scene_index": 1,
      "source_tier": "chapter_event",
      "traits": [],
      "status": "健康，保持警戒"
    },
    "陆遥": {
      "aliases": [
        "陆遥"
      ],
      "canonical_name": "陆遥",
      "character_id": "陆遥",
      "identity": "陆沉的妹妹",
      "role": "北站线索提供者",
      "source_tier": "story_project_standard",
      "status": "第18章三秒低功率握手中实时确认位于北站下层并提示先停掉节点四；具有自主意识，感染与受控情况未知",
      "field": "status",
      "before": "第17章确认真实呼吸声并主动使用老式防空警报载波；感染与受控情况未知",
      "after": "第18章三秒低功率握手中实时确认位于北站下层并提示先停掉节点四；具有自主意识，感染与受控情况未知",
      "reason": "三秒回信的声纹、呼吸节律与实时回波证明她不是循环录音且能自主回应，但不能证明未感染或未受控制",
      "scene_index": 4
    },
    "韩野": {
      "affiliation": "消防站幸存者",
      "aliases": [
        "韩野"
      ],
      "canonical_name": "韩野",
      "character_id": "韩野",
      "identity": "临江三号消防站原消防员",
      "role": "消防站八名幸存者临时负责人",
      "source_tier": "story_project_standard"
    },
    "顾岚": {
      "affiliation": "火种一号",
      "aliases": [
        "顾岚"
      ],
      "canonical_name": "顾岚",
      "character_id": "顾岚",
      "identity": "火种一号成员",
      "role": "武器登记与现场警戒",
      "source_tier": "story_project_standard",
      "field": "location",
      "before": "泵房检修门",
      "after": "梁魁办公室外接点",
      "reason": "顾岚沿泵房侧墙的信号线前往梁魁办公室外接点，复制装卸区广播回路中的加密目录后暂留该处",
      "scene_index": 3,
      "location": "梁魁办公室外接点"
    },
    "仓管员": {
      "character_id": "仓管员",
      "field": "status",
      "before": null,
      "after": "临时受保护者，可自愿作证，不参加战斗",
      "reason": "顾岚确认其受保护资格与自愿边界",
      "scene_index": 3,
      "canonical_name": "仓管员",
      "aliases": [
        "仓管员"
      ],
      "source_tier": "chapter_event",
      "status": "临时受保护者，可自愿作证，不参加战斗"
    }
  },
  "relationships": {
    "陆沉->韩野": {
      "after": "在院门防守中接受韩野的消防专业调度，并依靠云梯水带、联锁车与泄压沟的联合配合击杀披甲者，战术协作进一步稳定",
      "before": "首次完成并肩作战配合，但仍未形成完全信任",
      "combat_coordination": "在院门防守中接受韩野的消防专业调度，并依靠云梯水带、联锁车与泄压沟的联合配合击杀披甲者，战术协作进一步稳定",
      "field": "combat_coordination",
      "reason": "两人以明确分工完成对披甲者的协同处置，陆沉未脱离岗位单独正面硬杀",
      "relationship_id": "陆沉->韩野",
      "scene_index": 2,
      "source_character_id": "陆沉",
      "source_id": "陆沉",
      "source_tier": "chapter_event",
      "target_character_id": "韩野",
      "target_id": "韩野",
      "type": "relationship",
      "合作边界": "同意R-17铅盒进入站内独立隔离柜并设置放弃阈值，不要求消防站无条件信任或交出核心控制权",
      "source_event_id": "chapter-0020-beat-006"
    },
    "韩野->陆沉": {
      "after": "认可火种一号双向复核规则的有效性；因陆沉遵守三秒发射上限，同意共同调查消防站地下节点；见其将微孔滤膜仅封入试验滤筒而非人体，风险警惕仍在但对其自我限制进一步记录",
      "before": "认可火种一号双向复核规则的有效性；因陆沉遵守三秒发射上限，同意共同调查消防站地下节点，但仍保持风险警惕",
      "field": "信任评估",
      "reason": "陆沉在滤骨者掉落微孔滤膜后接受双方复核，只用于净化机组试验滤筒",
      "relationship_id": "韩野->陆沉",
      "scene_index": 3,
      "source_character_id": "韩野",
      "source_id": "韩野",
      "source_tier": "chapter_event",
      "target_character_id": "陆沉",
      "target_id": "陆沉",
      "threat_assessment": "确认陆沉未将静默腔膜装入人体，警惕未消但对其自我限制有所记录",
      "type": "relationship",
      "信任评估": "认可火种一号双向复核规则的有效性；因陆沉遵守三秒发射上限，同意共同调查消防站地下节点；见其将微孔滤膜仅封入试验滤筒而非人体，风险警惕仍在但对其自我限制进一步记录",
      "source_event_id": "chapter-0019-beat-007",
      "战术互信": "在节点四旧泵房内按陆沉弱点视界指示固定滤骨者，接受其污染隔离与不回流蓄水池的判断，但仍保留对方舟线索的审慎边界。"
    },
    "韩野->林夏": {
      "after": "确认陆沉缺席时林夏仍能按预案守住消防站院门，认可其地面调度权。",
      "before": null,
      "field": "地面调度认可",
      "reason": "黑旗前哨被击退后，下井组返回车库，韩野当面承认林夏的调度有效。",
      "source_event_id": "chapter-0019-beat-010",
      "source_id": "韩野",
      "target_id": "林夏",
      "scene_index": 4,
      "relationship_id": "韩野->林夏",
      "source_character_id": "韩野",
      "target_character_id": "林夏",
      "type": "relationship",
      "地面调度认可": "确认陆沉缺席时林夏仍能按预案守住消防站院门，认可其地面调度权。",
      "source_tier": "chapter_event"
    },
    "韩野->苏晴": {
      "after": "确认陆沉缺席时苏晴仍能维持水样、隔离线和受保护者自愿边界，认可其医疗隔离安排。",
      "before": null,
      "field": "医疗隔离认可",
      "reason": "下井组返回后，韩野看到水样封存、隔离线和四名受保护者仍单列，接受苏晴的安排。",
      "source_event_id": "chapter-0019-beat-010",
      "source_id": "韩野",
      "target_id": "苏晴",
      "scene_index": 4,
      "relationship_id": "韩野->苏晴",
      "source_character_id": "韩野",
      "target_character_id": "苏晴",
      "type": "relationship",
      "医疗隔离认可": "确认陆沉缺席时苏晴仍能维持水样、隔离线和受保护者自愿边界，认可其医疗隔离安排。",
      "source_tier": "chapter_event"
    }
  },
  "roster": {
    "roster_fire_station": {
      "aliases": [
        "消防站",
        "站内八人"
      ],
      "baseline_evidence": {
        "exact_evidence": [
          {
            "evidence_id": "fire-station-8",
            "line_number": 7,
            "text": "院门内，消防站原有八人分散在车库、卷帘控制箱和水带架旁。韩野站在半开的内侧铁门后，枪口压低，却没有放下。四名刚被救出来的人被单独隔在双门之间，背靠墙坐着，手腕上绑着临时编号，既没有算进火种一号，也没有被消防站接收。"
          }
        ],
        "sha256": "77a26c5acc0dff7dfbd413734c969703ba6e2e0cd945db30c34341bb4b286c20",
        "source_kind": "committed_chapter_prose",
        "source_path": "正文/第017章_第十七章：消防站不开门.md"
      },
      "baseline_source": ".novelagent/runtime/runs/chapter_17_20260728T032330670015Z.json",
      "computed_count": 8,
      "declared_count": 8,
      "introduced_chapter": 17,
      "introduced_event_id": "chapter-0017-beat-001",
      "members": [],
      "migration_id": "chapter17-roster-baseline-v1",
      "name": "消防站幸存者",
      "roster_id": "roster_fire_station",
      "source_tier": "story_project_standard",
      "unresolved_count": 8
    },
    "roster_fireseed_one": {
      "aliases": [
        "火种队"
      ],
      "baseline_evidence": {
        "exact_evidence": [
          {
            "evidence_id": "fireseed-17",
            "line_number": 5,
            "text": "陆沉站在器材口外，背后是从冷库一路压到这里的三辆联锁车。车头交错顶住街沿，车身之间挂着负载分流的钢索，最后一辆车的后座下固定着R-17铅盒，铅封没有动。火种一号十七人全部在门外，伤员靠内圈，持械的人靠外圈，没人越过韩野画在地上的白灰线。"
          }
        ],
        "sha256": "77a26c5acc0dff7dfbd413734c969703ba6e2e0cd945db30c34341bb4b286c20",
        "source_kind": "committed_chapter_prose",
        "source_path": "正文/第017章_第十七章：消防站不开门.md"
      },
      "baseline_source": ".novelagent/runtime/runs/chapter_17_20260728T032330670015Z.json",
      "computed_count": 17,
      "declared_count": 17,
      "introduced_chapter": 17,
      "introduced_event_id": "chapter-0017-beat-001",
      "members": [],
      "migration_id": "chapter17-roster-baseline-v1",
      "name": "火种一号",
      "roster_id": "roster_fireseed_one",
      "source_tier": "story_project_standard",
      "unresolved_count": 17
    },
    "roster_rescued_observation": {
      "aliases": [
        "获救者",
        "待检者",
        "待检四人"
      ],
      "baseline_evidence": {
        "exact_evidence": [
          {
            "evidence_id": "rescued-observation-4",
            "line_number": 181,
            "text": "四名获救者被单独引进旁侧观察间。门一关，林夏在白板上写下“两小时”，又在名字后划了空格：“他们暂不算火种一号，也不算消防站。”"
          }
        ],
        "sha256": "77a26c5acc0dff7dfbd413734c969703ba6e2e0cd945db30c34341bb4b286c20",
        "source_kind": "committed_chapter_prose",
        "source_path": "正文/第017章_第十七章：消防站不开门.md"
      },
      "baseline_source": ".novelagent/runtime/runs/chapter_17_20260728T032330670015Z.json",
      "computed_count": 12,
      "declared_count": 12,
      "introduced_chapter": 17,
      "introduced_event_id": "chapter-0017-beat-008",
      "members": [],
      "migration_id": "chapter17-roster-baseline-v1",
      "name": "获救者观察组",
      "roster_id": "roster_rescued_observation",
      "source_tier": "story_project_standard",
      "unresolved_count": 12
    }
  },
  "numeric_counters": {
    "陆沉侵蚀值": {
      "after": 6,
      "before": 6,
      "counter_id": "陆沉侵蚀值",
      "current_value": 6,
      "declared_value": 6,
      "delta": 0,
      "expected_value": 6,
      "label": "陆沉侵蚀值",
      "maximum": 100,
      "minimum": 0,
      "owner_id": "陆沉",
      "previous_value": 6,
      "reason": "陆沉使用一次弱点视界后未出现新增侵蚀，侵蚀值保持不变",
      "rule": "monotonic_non_decreasing",
      "scene_index": 1,
      "source_event_id": "chapter-0024-beat-002",
      "source_tier": "chapter_event"
    }
  },
  "inventory": {
    "通信室与警铃管路之间的隔音箱:静默腔膜": {
      "after": 1,
      "before": 0,
      "declared_quantity": 1,
      "delta": 1,
      "inventory_id": "通信室与警铃管路之间的隔音箱:静默腔膜",
      "item_id": "静默腔膜",
      "owner_id": "通信室与警铃管路之间的隔音箱",
      "previous_quantity": 0,
      "quantity": 1,
      "reason": "鸣腔者掉落的蓝色素材被陆沉赋予并安装到隔音箱内侧",
      "scene_index": 7,
      "source_event_id": "chapter-0017-beat-007",
      "source_tier": "chapter_event"
    },
    "顾岚:纸质出库联": {
      "owner_id": "顾岚",
      "item_id": "纸质出库联",
      "before": 0,
      "delta": 1,
      "after": 1,
      "reason": "顾岚封存纸质原件作为证据",
      "source_event_id": "chapter-0024-beat-005",
      "scene_index": 3,
      "previous_quantity": 0,
      "declared_quantity": 1,
      "inventory_id": "顾岚:纸质出库联",
      "quantity": 1,
      "source_tier": "chapter_event"
    },
    "仓管员:离线备份": {
      "owner_id": "仓管员",
      "item_id": "离线备份",
      "before": 0,
      "delta": 1,
      "after": 1,
      "reason": "离线副本交由仓管员保管，与纸质原件分开保存",
      "source_event_id": "chapter-0024-beat-005",
      "scene_index": 3,
      "previous_quantity": 0,
      "declared_quantity": 1,
      "inventory_id": "仓管员:离线备份",
      "quantity": 1,
      "source_tier": "chapter_event"
    },
    "火种一号:M-17-04主滤芯": {
      "owner_id": "火种一号",
      "item_id": "M-17-04主滤芯",
      "before": 0,
      "delta": 1,
      "after": 1,
      "reason": "外勤组三人夺下真滤芯车，确认一组封装完好的主滤芯并固定封存",
      "source_event_id": "chapter-0025-beat-005",
      "scene_index": 3,
      "previous_quantity": 0,
      "declared_quantity": 1,
      "inventory_id": "火种一号:M-17-04主滤芯",
      "quantity": 1,
      "source_tier": "chapter_event"
    },
    "火种一号:M-17-05主滤芯": {
      "owner_id": "火种一号",
      "item_id": "M-17-05主滤芯",
      "before": 0,
      "delta": 1,
      "after": 1,
      "reason": "外勤组三人夺下真滤芯车，确认第二组主滤芯但其外壳开裂，贴待检封条，不作为完好备用",
      "source_event_id": "chapter-0025-beat-005",
      "scene_index": 3,
      "previous_quantity": 0,
      "declared_quantity": 1,
      "inventory_id": "火种一号:M-17-05主滤芯",
      "quantity": 1,
      "source_tier": "chapter_event"
    }
  },
  "locations": {
    "R-17铅盒": {
      "after": "消防站独立隔离柜",
      "before": null,
      "entity_id": "R-17铅盒",
      "location_id": "消防站独立隔离柜",
      "reason": "陆沉与韩野达成风险隔离协议后，由双方复核转入站内独立隔离柜",
      "scene_index": 10,
      "source_tier": "chapter_event"
    },
    "三辆联锁车": {
      "after": "消防站器材口外负载分流防线",
      "before": null,
      "entity_id": "三辆联锁车",
      "location_id": "消防站器材口外负载分流防线",
      "reason": "火种一号将三辆联锁车接成三角并顶住器材口外侧以抵挡感染者冲击",
      "scene_index": 5,
      "source_tier": "chapter_event"
    },
    "四名获救者": {
      "after": "单独两小时观察区",
      "before": "双门隔离区",
      "entity_id": "四名获救者",
      "location_id": "单独两小时观察区",
      "reason": "四名获救者验伤后被转入旁侧观察间进行单独两小时观察，不并入任一组织",
      "scene_index": 8,
      "source_tier": "chapter_event"
    },
    "林夏": {
      "certainty": "confirmed",
      "entity_id": "林夏",
      "last_reported_chapter": 17,
      "location_id": "消防站一层车库区",
      "source_tier": "chapter_event",
      "status": "current",
      "after": "消防站一层车库区",
      "before": "消防站器材口外负载分流防线",
      "reason": "战后返回车库主持公开登记",
      "scene_index": 3
    },
    "火种一号": {
      "after": "消防站一层车库区",
      "before": null,
      "entity_id": "火种一号",
      "location_id": "消防站一层车库区",
      "reason": "火种一号十七人经双向验伤后被允许进入消防站一层车库区临时避险",
      "scene_index": 8,
      "source_tier": "chapter_event"
    },
    "苏晴": {
      "certainty": "confirmed",
      "entity_id": "苏晴",
      "last_reported_chapter": 17,
      "location_id": "消防站一层车库区",
      "source_tier": "chapter_event",
      "status": "current"
    },
    "赵铁": {
      "after": "物流园外围维修道",
      "before": "无信号的装卸检修间",
      "entity_id": "赵铁",
      "location_id": "物流园外围维修道",
      "reason": "赵铁打开并带队通过消防通道撤出冷库",
      "scene_index": 4,
      "source_tier": "chapter_event"
    },
    "陆沉": {
      "after": "物流园外围维修道",
      "before": "无信号的装卸检修间",
      "entity_id": "陆沉",
      "location_id": "物流园外围维修道",
      "reason": "陆沉沿八名获救者指认的消防通道撤出冷库",
      "scene_index": 4,
      "source_tier": "chapter_event"
    },
    "陆遥": {
      "certainty": "self_reported",
      "entity_id": "陆遥",
      "last_reported_chapter": 10,
      "location_id": "北站地下轨道检修层下方",
      "source_tier": "chapter_event",
      "status": "unverified"
    },
    "韩野": {
      "after": "消防站一层车库区",
      "before": "消防站器材口外负载分流防线",
      "entity_id": "韩野",
      "location_id": "消防站一层车库区",
      "reason": "停止追击后返回车库检验防护盾并参加公开登记",
      "scene_index": 3,
      "source_tier": "chapter_event"
    },
    "顾岚": {
      "certainty": "confirmed",
      "entity_id": "顾岚",
      "last_reported_chapter": 17,
      "location_id": "物流园外围维修道",
      "source_tier": "chapter_event",
      "status": "current",
      "before": "无信号的装卸检修间",
      "after": "物流园外围维修道",
      "reason": "顾岚护送获救者并携带证物通过消防通道撤出冷库",
      "scene_index": 4
    },
    "seeker": {
      "entity_id": "seeker",
      "before": null,
      "after": "消防站院内",
      "reason": "受高频刺激沿外墙攀向通信塔，随后被陆沉击杀后坠落在院内",
      "scene_index": 3,
      "location_id": "消防站院内",
      "source_tier": "chapter_event"
    },
    "roof-independent-manual-bell-pull": {
      "entity_id": "roof-independent-manual-bell-pull",
      "before": null,
      "after": "消防站屋顶",
      "reason": "被确认为屋顶机械警铃的独立遗留拉线，位置在屋顶旧钟架旁",
      "scene_index": 3,
      "location_id": "消防站屋顶",
      "source_tier": "chapter_event"
    },
    "communication-operator": {
      "entity_id": "communication-operator",
      "before": null,
      "after": "二层通信室",
      "reason": "通信员按林夏命令留在二层通信室门内待命，没有被派往塔顶",
      "scene_index": 3,
      "location_id": "二层通信室",
      "source_tier": "chapter_event"
    },
    "armored-one": {
      "after": "消防站器材口外负载分流防线",
      "before": null,
      "entity_id": "armored-one",
      "reason": "被黑旗前哨从链缚尸群后方释放并倒毙于院门缓冲线",
      "scene_index": 2,
      "location_id": "消防站器材口外负载分流防线",
      "source_tier": "chapter_event"
    },
    "layered-bone-armor": {
      "after": "消防站一层车库区",
      "before": null,
      "entity_id": "layered-bone-armor",
      "reason": "披甲者掉落的蓝色词条被赋予消防防护盾与车库内门",
      "scene_index": 3,
      "location_id": "消防站一层车库区",
      "source_tier": "chapter_event"
    },
    "高压泵运输车": {
      "after": "消防站一层车库区",
      "before": null,
      "entity_id": "高压泵运输车",
      "reason": "经外观检查和爆炸物排查后被隔离拖入车库",
      "scene_index": 3,
      "location_id": "消防站一层车库区",
      "source_tier": "chapter_event"
    },
    "消防救援车": {
      "entity_id": "消防救援车",
      "before": "城西物流园方向通勤道",
      "after": "物流园外围维修道",
      "reason": "救援车进入物流园外围维修道后弃车步行",
      "scene_index": 1,
      "location_id": "物流园外围维修道",
      "source_tier": "chapter_event"
    },
    "屏蔽箱": {
      "entity_id": "屏蔽箱",
      "before": "东街支路",
      "after": "泵房检修门",
      "reason": "赵铁携屏蔽箱抵达泵房检修门并取出应答器完成门禁握手",
      "scene_index": 1,
      "location_id": "泵房检修门",
      "source_tier": "chapter_event"
    },
    "泵车应答器": {
      "entity_id": "泵车应答器",
      "before": "东街支路",
      "after": "泵房检修门",
      "reason": "赵铁在泵房检修门完成一次门禁握手后立即断开应答器电源",
      "scene_index": 1,
      "location_id": "泵房检修门",
      "source_tier": "chapter_event"
    },
    "救援车绞盘": {
      "entity_id": "救援车绞盘",
      "before": null,
      "after": "旧通勤道",
      "reason": "赵铁从停在旧通勤道的消防救援车上拖出绞盘并用于稳定废弃通勤车",
      "scene_index": 3,
      "location_id": "旧通勤道",
      "source_tier": "chapter_event"
    },
    "弹性筋腱束带": {
      "entity_id": "弹性筋腱束带",
      "before": null,
      "after": "旧通勤道",
      "reason": "赵铁在旧通勤道使用两条弹性筋腱束带固定通勤车车架",
      "scene_index": 3,
      "location_id": "旧通勤道",
      "source_tier": "chapter_event"
    },
    "消防救援车金属器材柜": {
      "entity_id": "消防救援车金属器材柜",
      "before": null,
      "after": "旧通勤道",
      "reason": "器材柜随消防救援车位于旧通勤道并用于封存应答片",
      "scene_index": 3,
      "location_id": "旧通勤道",
      "source_tier": "chapter_event"
    },
    "四枚R-17应答片": {
      "entity_id": "四枚R-17应答片",
      "before": null,
      "after": "旧通勤道",
      "reason": "四枚应答片从获救者身上拆除后封入旧通勤道救援车的金属器材柜",
      "scene_index": 3,
      "location_id": "旧通勤道",
      "source_tier": "chapter_event"
    },
    "四名新临时受保护者": {
      "entity_id": "四名新临时受保护者",
      "before": "旧通勤道",
      "after": "消防站双门验伤区",
      "reason": "四人由两名已登记常驻者驾驶的接应车从安全交汇点送回消防站，并进入苏晴安排的双门验伤流程",
      "scene_index": 4,
      "location_id": "消防站双门验伤区",
      "source_tier": "chapter_event"
    },
    "八名获救者": {
      "entity_id": "八名获救者",
      "before": "物流园外围维修道",
      "after": "无信号的装卸检修间",
      "reason": "八名刚救出的伤者留在检修间接受看护，不参加追车",
      "scene_index": 1,
      "location_id": "无信号的装卸检修间",
      "source_tier": "chapter_event"
    },
    "仓管员": {
      "entity_id": "仓管员",
      "before": "物流园外围维修道",
      "after": "无信号的装卸检修间",
      "reason": "仓管员返回已断信号的检修间，与自愿留下的非战斗人员共同看护获救者",
      "scene_index": 1,
      "location_id": "无信号的装卸检修间",
      "source_tier": "chapter_event"
    }
  },
  "events": {
    "chapter-0017-beat-001": {
      "event_id": "chapter-0017-beat-001",
      "location": "消防站器材口外",
      "objects": [
        "火警主机",
        "三辆联锁车",
        "R-17铅盒"
      ],
      "scene_index": 1,
      "source_tier": "chapter_event",
      "status": "completed",
      "subjects": [
        "陆沉",
        "苏晴",
        "韩野",
        "火种一号",
        "消防站原有八人",
        "四名获救者"
      ],
      "type": "required_beat_1_completed"
    },
    "chapter-0017-beat-002": {
      "event_id": "chapter-0017-beat-002",
      "location": "消防站器材口外",
      "objects": [
        "火警主机",
        "二层通信室",
        "车库卷帘",
        "应急蓄水泵",
        "器材口外接线箱",
        "警报功放",
        "主机触发线"
      ],
      "scene_index": 2,
      "source_tier": "chapter_event",
      "status": "completed",
      "subjects": [
        "韩野",
        "赵铁",
        "陆沉"
      ],
      "type": "required_beat_2_completed"
    },
    "chapter-0017-beat-003": {
      "event_id": "chapter-0017-beat-003",
      "location": "消防站器材口外",
      "objects": [
        "验伤检查项目",
        "武器登记",
        "开门复核规则"
      ],
      "scene_index": 3,
      "source_tier": "chapter_event",
      "status": "completed",
      "subjects": [
        "苏晴",
        "站内护士",
        "顾岚",
        "韩野",
        "林夏",
        "火种一号",
        "消防站原有八人",
        "四名获救者"
      ],
      "type": "required_beat_3_completed"
    },
    "chapter-0017-beat-004": {
      "event_id": "chapter-0017-beat-004",
      "location": "消防站器材口外及二层通信室",
      "objects": [
        "黑旗会改装火警线路",
        "机械回弹装置",
        "屋顶旧警铃",
        "外部功放",
        "火警主机",
        "回弹拉索",
        "弱点视界",
        "折叠钩矛"
      ],
      "scene_index": 4,
      "source_tier": "chapter_event",
      "status": "completed",
      "subjects": [
        "陆沉",
        "赵铁",
        "韩野"
      ],
      "type": "required_beat_4_completed"
    },
    "chapter-0017-beat-005": {
      "event_id": "chapter-0017-beat-005",
      "location": "消防站器材口外及院内",
      "objects": [
        "三辆联锁车",
        "负载分流",
        "消防站器材口",
        "泡沫管线",
        "防火卷帘",
        "双门隔离区",
        "消防站院门"
      ],
      "scene_index": 5,
      "source_tier": "chapter_event",
      "status": "completed",
      "subjects": [
        "火种一号",
        "消防站原有八人",
        "四名获救者"
      ],
      "type": "required_beat_5_completed"
    },
    "chapter-0017-beat-006": {
      "event_id": "chapter-0017-beat-006",
      "location": "消防站器材口外及院内",
      "objects": [
        "鸣腔者",
        "泡沫管线",
        "防火卷帘",
        "折叠钩矛",
        "弱点视界",
        "消防站院门"
      ],
      "scene_index": 6,
      "source_tier": "chapter_event",
      "status": "completed",
      "subjects": [
        "陆沉",
        "韩野",
        "赵铁"
      ],
      "type": "required_beat_6_completed"
    },
    "chapter-0017-beat-007": {
      "event_id": "chapter-0017-beat-007",
      "location": "二层通信室",
      "objects": [
        "鸣腔者",
        "静默腔膜",
        "通信室与警铃管路之间的隔音箱",
        "短波通信"
      ],
      "scene_index": 7,
      "source_tier": "chapter_event",
      "status": "completed",
      "subjects": [
        "陆沉",
        "韩野"
      ],
      "type": "required_beat_7_completed"
    },
    "chapter-0017-beat-008": {
      "event_id": "chapter-0017-beat-008",
      "location": "消防站一层车库区",
      "objects": [
        "双向验伤记录",
        "临时避险区"
      ],
      "scene_index": 8,
      "source_tier": "chapter_event",
      "status": "completed",
      "subjects": [
        "陆沉",
        "韩野",
        "火种一号"
      ],
      "type": "required_beat_8_completed"
    },
    "chapter-0017-beat-009": {
      "event_id": "chapter-0017-beat-009",
      "location": "消防站一层车库区",
      "objects": [
        "地下蓄水池",
        "净化机组",
        "高压泵",
        "主滤芯",
        "柴油消防车",
        "泡沫剂",
        "站内食物"
      ],
      "scene_index": 9,
      "source_tier": "chapter_event",
      "status": "completed",
      "subjects": [
        "韩野",
        "陆沉",
        "苏晴",
        "林夏",
        "赵铁",
        "顾岚",
        "站内护士",
        "火种一号"
      ],
      "type": "required_beat_9_completed"
    },
    "chapter-0017-beat-010": {
      "event_id": "chapter-0017-beat-010",
      "location": "消防站一层车库区",
      "objects": [
        "R-17铅盒",
        "独立隔离柜",
        "放弃阈值",
        "声纹比对器",
        "陆遥真实呼吸声",
        "老式防空警报"
      ],
      "scene_index": 10,
      "source_tier": "chapter_event",
      "status": "completed",
      "subjects": [
        "陆沉",
        "韩野",
        "顾岚"
      ],
      "type": "required_beat_10_completed"
    },
    "history-chapter-0004-dashcam-siren-warning": {
      "event_id": "history-chapter-0004-dashcam-siren-warning",
      "location": "北站线路行车记录仪",
      "objects": [
        "行车记录仪",
        "老式防空警报",
        "陆沉别来北站"
      ],
      "source_chapter": 4,
      "source_tier": "chapter_event",
      "status": "completed",
      "subjects": [
        "陆遥",
        "陆沉"
      ],
      "type": "recorded_signal_match"
    },
    "history-chapter-0004-luyao-last-voice-siren": {
      "event_id": "history-chapter-0004-luyao-last-voice-siren",
      "evidence_scope": "现有正文第4章首次逐字可核验；不得倒填为第1章正文事件",
      "location": "来源地点未证实",
      "objects": [
        "爆发前最后语音",
        "老式防空警报",
        "一长两短间隔"
      ],
      "source_chapter": 4,
      "source_tier": "chapter_event",
      "status": "completed",
      "subjects": [
        "陆遥",
        "陆沉"
      ],
      "type": "historical_signal_observation"
    },
    "history-chapter-0005-luchen-equipped-rotting-odor-screen": {
      "event_id": "history-chapter-0005-luchen-equipped-rotting-odor-screen",
      "location": "地下商超",
      "objects": [
        "腐味屏蔽（蓝）"
      ],
      "source_chapter": 5,
      "source_tier": "chapter_event",
      "status": "completed",
      "subjects": [
        "陆沉"
      ],
      "type": "ability_equipped"
    },
    "history-chapter-0008-r17-radio-warning": {
      "event_id": "history-chapter-0008-r17-radio-warning",
      "location": "人防医院地下病区控制间",
      "objects": [
        "老式电台",
        "与R-17回收信标同源的防空警报",
        "R-17核心离开回收区后的追踪警告"
      ],
      "source_chapter": 8,
      "source_tier": "chapter_event",
      "status": "completed",
      "subjects": [
        "赵铁",
        "陆遥"
      ],
      "type": "r17_radio_intercept"
    },
    "history-chapter-0009-r17-wired-control": {
      "event_id": "history-chapter-0009-r17-wired-control",
      "location": "人防医院设备层",
      "objects": [
        "R-17运输网",
        "加密有线控制回路",
        "病区广播",
        "门锁"
      ],
      "source_chapter": 9,
      "source_tier": "chapter_event",
      "status": "completed",
      "subjects": [
        "赵铁"
      ],
      "type": "infrastructure_discovery"
    },
    "history-chapter-0010-luyao-north-station-report": {
      "event_id": "history-chapter-0010-luyao-north-station-report",
      "location": "北站地下轨道检修层下方（陆遥自报）",
      "objects": [
        "北站地下",
        "宿主名单警告"
      ],
      "source_chapter": 10,
      "source_tier": "chapter_event",
      "status": "completed",
      "subjects": [
        "陆遥",
        "陆沉"
      ],
      "type": "radio_location_report"
    },
    "chapter-0018-beat-001": {
      "event_id": "chapter-0018-beat-001",
      "type": "required_beat_1_completed",
      "subjects": [
        "陆沉",
        "韩野",
        "顾岚",
        "林夏",
        "苏晴",
        "赵铁"
      ],
      "objects": [],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 1,
      "source_tier": "chapter_event"
    },
    "chapter-0018-beat-002": {
      "event_id": "chapter-0018-beat-002",
      "type": "required_beat_2_completed",
      "subjects": [
        "赵铁",
        "林夏",
        "韩野",
        "陆沉"
      ],
      "objects": [
        "爆发前最后语音",
        "第4章车载录音",
        "第8章电台呼叫",
        "消防站旧调度记录",
        "老式防空警报"
      ],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 1,
      "source_tier": "chapter_event"
    },
    "chapter-0018-beat-003": {
      "event_id": "chapter-0018-beat-003",
      "type": "required_beat_3_completed",
      "subjects": [
        "苏晴",
        "顾岚",
        "林夏",
        "陆沉"
      ],
      "objects": [
        "病区日志",
        "民防警报",
        "R-17民防广播载波"
      ],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 1,
      "source_tier": "chapter_event"
    },
    "chapter-0018-beat-004": {
      "event_id": "chapter-0018-beat-004",
      "type": "required_beat_4_completed",
      "subjects": [
        "陆沉",
        "苏晴",
        "赵铁",
        "林夏",
        "韩野",
        "顾岚"
      ],
      "objects": [
        "北站地下民防广播支线",
        "R-17运输网",
        "老式防空警报"
      ],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 2,
      "source_tier": "chapter_event"
    },
    "chapter-0018-beat-005": {
      "event_id": "chapter-0018-beat-005",
      "type": "required_beat_5_completed",
      "subjects": [
        "陆沉",
        "韩野",
        "林夏",
        "苏晴",
        "赵铁",
        "顾岚"
      ],
      "objects": [
        "消防站调度塔",
        "静默腔膜"
      ],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 2,
      "source_tier": "chapter_event"
    },
    "chapter-0018-beat-006": {
      "event_id": "chapter-0018-beat-006",
      "type": "required_beat_6_completed",
      "subjects": [
        "韩野"
      ],
      "objects": [
        "roof-independent-manual-bell-pull",
        "屋顶机械警铃",
        "备用线路"
      ],
      "location": "消防站屋顶",
      "status": "completed",
      "scene_index": 3,
      "source_tier": "chapter_event"
    },
    "chapter-0018-beat-007": {
      "event_id": "chapter-0018-beat-007",
      "type": "required_beat_7_completed",
      "subjects": [
        "陆沉",
        "赵铁"
      ],
      "objects": [
        "seeker",
        "消防云梯",
        "腐味屏蔽（蓝）",
        "弱点视界",
        "折叠钩矛"
      ],
      "location": "消防站院内",
      "status": "completed",
      "scene_index": 3,
      "source_tier": "chapter_event"
    },
    "chapter-0018-beat-008": {
      "event_id": "chapter-0018-beat-008",
      "type": "required_beat_8_completed",
      "subjects": [
        "communication-operator"
      ],
      "objects": [
        "node-four"
      ],
      "location": "二层通信室",
      "status": "completed",
      "scene_index": 4,
      "source_tier": "chapter_event"
    },
    "chapter-0018-beat-009": {
      "event_id": "chapter-0018-beat-009",
      "type": "required_beat_9_completed",
      "subjects": [
        "陆沉",
        "韩野",
        "苏晴",
        "林夏",
        "顾岚",
        "赵铁"
      ],
      "objects": [
        "node-four"
      ],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 4,
      "source_tier": "chapter_event"
    },
    "chapter-0019-beat-001": {
      "event_id": "chapter-0019-beat-001",
      "type": "required_beat_1_completed",
      "subjects": [
        "林夏",
        "苏晴",
        "赵铁",
        "陆沉",
        "韩野",
        "顾岚"
      ],
      "objects": [],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 1,
      "source_tier": "chapter_event"
    },
    "chapter-0019-beat-002": {
      "event_id": "chapter-0019-beat-002",
      "type": "required_beat_2_completed",
      "subjects": [
        "苏晴",
        "赵铁"
      ],
      "objects": [],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 1,
      "source_tier": "chapter_event"
    },
    "chapter-0019-beat-003": {
      "event_id": "chapter-0019-beat-003",
      "type": "required_beat_3_completed",
      "subjects": [
        "陆沉",
        "赵铁",
        "韩野",
        "顾岚"
      ],
      "objects": [],
      "location": "阀门廊",
      "status": "completed",
      "scene_index": 1,
      "source_tier": "chapter_event"
    },
    "chapter-0019-beat-004": {
      "event_id": "chapter-0019-beat-004",
      "type": "required_beat_4_completed",
      "subjects": [
        "陆沉",
        "赵铁",
        "韩野",
        "顾岚"
      ],
      "objects": [
        "泵房钥匙"
      ],
      "location": "阀门廊",
      "status": "completed",
      "scene_index": 2,
      "source_tier": "chapter_event"
    },
    "chapter-0019-beat-005": {
      "event_id": "chapter-0019-beat-005",
      "type": "required_beat_5_completed",
      "subjects": [
        "陆沉",
        "赵铁",
        "韩野",
        "顾岚"
      ],
      "objects": [
        "宿主名单终端"
      ],
      "location": "节点四旧泵房",
      "status": "completed",
      "scene_index": 2,
      "source_tier": "chapter_event"
    },
    "chapter-0019-beat-006": {
      "event_id": "chapter-0019-beat-006",
      "type": "required_beat_6_completed",
      "subjects": [
        "陆沉",
        "赵铁",
        "韩野",
        "顾岚"
      ],
      "objects": [
        "filter-bone",
        "旧滤芯",
        "透明检修段",
        "手摇泵"
      ],
      "location": "节点四旧泵房",
      "status": "completed",
      "scene_index": 2,
      "source_tier": "chapter_event"
    },
    "chapter-0019-beat-007": {
      "event_id": "chapter-0019-beat-007",
      "type": "required_beat_7_completed",
      "subjects": [
        "陆沉",
        "赵铁",
        "韩野",
        "顾岚"
      ],
      "objects": [
        "filter-bone",
        "micro-porous-filter-membrane"
      ],
      "location": "节点四旧泵房",
      "status": "completed",
      "scene_index": 3,
      "source_tier": "chapter_event"
    },
    "chapter-0019-beat-008": {
      "event_id": "chapter-0019-beat-008",
      "type": "required_beat_8_completed",
      "subjects": [
        "林夏",
        "苏晴"
      ],
      "objects": [],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 3,
      "source_tier": "chapter_event"
    },
    "chapter-0019-beat-009": {
      "event_id": "chapter-0019-beat-009",
      "type": "required_beat_9_completed",
      "subjects": [
        "陆沉",
        "赵铁",
        "韩野",
        "顾岚"
      ],
      "objects": [],
      "location": "节点四旧泵房",
      "status": "completed",
      "scene_index": 4,
      "source_tier": "chapter_event"
    },
    "chapter-0019-beat-010": {
      "event_id": "chapter-0019-beat-010",
      "type": "required_beat_10_completed",
      "subjects": [
        "陆沉",
        "赵铁",
        "韩野",
        "顾岚",
        "林夏",
        "苏晴"
      ],
      "objects": [],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 4,
      "source_tier": "chapter_event"
    },
    "chapter-0020-beat-001": {
      "event_id": "chapter-0020-beat-001",
      "type": "required_beat_1_completed",
      "subjects": [
        "林夏",
        "苏晴",
        "赵铁",
        "陆沉",
        "韩野",
        "顾岚"
      ],
      "objects": [
        "火种一号",
        "净化机组"
      ],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 1,
      "source_tier": "chapter_event"
    },
    "chapter-0020-beat-002": {
      "event_id": "chapter-0020-beat-002",
      "type": "required_beat_2_completed",
      "subjects": [
        "林夏",
        "苏晴",
        "赵铁",
        "陆沉",
        "韩野",
        "顾岚"
      ],
      "objects": [
        "火种一号"
      ],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 1,
      "source_tier": "chapter_event"
    },
    "chapter-0020-beat-003": {
      "event_id": "chapter-0020-beat-003",
      "type": "required_beat_3_completed",
      "subjects": [
        "林夏",
        "苏晴",
        "赵铁",
        "陆沉",
        "韩野",
        "顾岚"
      ],
      "objects": [
        "撤离阈值"
      ],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 1,
      "source_tier": "chapter_event"
    },
    "chapter-0020-beat-004": {
      "event_id": "chapter-0020-beat-004",
      "type": "required_beat_4_completed",
      "subjects": [
        "韩野",
        "赵铁",
        "林夏",
        "陆沉",
        "顾岚"
      ],
      "objects": [
        "三辆联锁车"
      ],
      "location": "消防站器材口外负载分流防线",
      "status": "completed",
      "scene_index": 2,
      "source_tier": "chapter_event"
    },
    "chapter-0020-beat-005": {
      "event_id": "chapter-0020-beat-005",
      "type": "required_beat_5_completed",
      "subjects": [
        "陆沉"
      ],
      "objects": [
        "armored-one"
      ],
      "location": "消防站器材口外负载分流防线",
      "status": "completed",
      "scene_index": 2,
      "source_tier": "chapter_event"
    },
    "chapter-0020-beat-006": {
      "event_id": "chapter-0020-beat-006",
      "type": "required_beat_6_completed",
      "subjects": [
        "韩野",
        "赵铁",
        "顾岚",
        "陆沉"
      ],
      "objects": [
        "armored-one",
        "三辆联锁车"
      ],
      "location": "消防站器材口外负载分流防线",
      "status": "completed",
      "scene_index": 2,
      "source_tier": "chapter_event"
    },
    "chapter-0020-beat-007": {
      "event_id": "chapter-0020-beat-007",
      "type": "required_beat_7_completed",
      "subjects": [
        "陆沉",
        "韩野"
      ],
      "objects": [
        "layered-bone-armor"
      ],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 3,
      "source_tier": "chapter_event"
    },
    "chapter-0020-beat-008": {
      "event_id": "chapter-0020-beat-008",
      "type": "required_beat_8_completed",
      "subjects": [
        "赵铁"
      ],
      "objects": [
        "高压泵运输车"
      ],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 3,
      "source_tier": "chapter_event"
    },
    "chapter-0020-beat-009": {
      "event_id": "chapter-0020-beat-009",
      "type": "required_beat_9_completed",
      "subjects": [
        "林夏",
        "苏晴",
        "陆沉",
        "韩野"
      ],
      "objects": [
        "fireseed-camp-fire-station-outpost"
      ],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 3,
      "source_tier": "chapter_event"
    },
    "chapter-0020-beat-010": {
      "event_id": "chapter-0020-beat-010",
      "type": "required_beat_10_completed",
      "subjects": [
        "林夏",
        "韩野",
        "陆沉",
        "苏晴",
        "赵铁"
      ],
      "objects": [],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 4,
      "source_tier": "chapter_event"
    },
    "chapter-0020-beat-011": {
      "event_id": "chapter-0020-beat-011",
      "type": "required_beat_11_completed",
      "subjects": [
        "林夏",
        "韩野",
        "陆沉",
        "苏晴",
        "赵铁"
      ],
      "objects": [],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 4,
      "source_tier": "chapter_event"
    },
    "chapter-0021-beat-001": {
      "event_id": "chapter-0021-beat-001",
      "type": "required_beat_1_completed",
      "subjects": [
        "林夏",
        "苏晴",
        "赵铁",
        "陆沉",
        "韩野",
        "顾岚"
      ],
      "objects": [
        "高压泵运输车",
        "净化机组"
      ],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 1,
      "source_tier": "chapter_event"
    },
    "chapter-0021-beat-002": {
      "event_id": "chapter-0021-beat-002",
      "type": "required_beat_2_completed",
      "subjects": [
        "赵铁",
        "韩野",
        "陆沉"
      ],
      "objects": [
        "高压泵运输车",
        "节点四终端"
      ],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 1,
      "source_tier": "chapter_event"
    },
    "chapter-0021-beat-003": {
      "event_id": "chapter-0021-beat-003",
      "type": "required_beat_3_completed",
      "subjects": [
        "陆沉",
        "赵铁",
        "韩野",
        "顾岚",
        "林夏"
      ],
      "objects": [
        "高压泵运输车",
        "节点四终端"
      ],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 2,
      "source_tier": "chapter_event"
    },
    "chapter-0021-beat-004": {
      "event_id": "chapter-0021-beat-004",
      "type": "required_beat_4_completed",
      "subjects": [
        "林夏",
        "苏晴",
        "陆沉",
        "赵铁",
        "韩野",
        "顾岚"
      ],
      "objects": [],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 2,
      "source_tier": "chapter_event"
    },
    "chapter-0021-beat-005": {
      "event_id": "chapter-0021-beat-005",
      "type": "required_beat_5_completed",
      "subjects": [
        "林夏",
        "苏晴",
        "赵铁",
        "陆沉",
        "韩野",
        "顾岚"
      ],
      "objects": [
        "三份方案表",
        "值勤板"
      ],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 3,
      "source_tier": "chapter_event"
    },
    "chapter-0021-beat-006": {
      "event_id": "chapter-0021-beat-006",
      "type": "required_beat_6_completed",
      "subjects": [
        "林夏",
        "苏晴",
        "赵铁",
        "陆沉",
        "韩野",
        "顾岚"
      ],
      "objects": [
        "消防救援车",
        "泵车应答器",
        "屏蔽箱"
      ],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 3,
      "source_tier": "chapter_event"
    },
    "chapter-0021-beat-007": {
      "event_id": "chapter-0021-beat-007",
      "type": "required_beat_7_completed",
      "subjects": [
        "林夏",
        "苏晴",
        "韩野",
        "顾岚",
        "赵铁"
      ],
      "objects": [
        "高压泵运输车",
        "layered-bone-armor"
      ],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 4,
      "source_tier": "chapter_event"
    },
    "chapter-0021-beat-008": {
      "event_id": "chapter-0021-beat-008",
      "type": "required_beat_8_completed",
      "subjects": [
        "陆沉",
        "林夏",
        "苏晴"
      ],
      "objects": [],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 4,
      "source_tier": "chapter_event"
    },
    "chapter-0022-beat-001": {
      "event_id": "chapter-0022-beat-001",
      "type": "required_beat_1_completed",
      "subjects": [
        "陆沉",
        "赵铁",
        "顾岚"
      ],
      "objects": [
        "消防救援车",
        "屏蔽箱"
      ],
      "location": "东街支路",
      "status": "completed",
      "scene_index": 1,
      "source_tier": "chapter_event"
    },
    "chapter-0022-beat-002": {
      "event_id": "chapter-0022-beat-002",
      "type": "required_beat_2_completed",
      "subjects": [
        "陆沉",
        "赵铁",
        "顾岚"
      ],
      "objects": [
        "消防救援车",
        "泵车应答器",
        "屏蔽箱"
      ],
      "location": "东街支路",
      "status": "completed",
      "scene_index": 1,
      "source_tier": "chapter_event"
    },
    "chapter-0022-beat-003": {
      "event_id": "chapter-0022-beat-003",
      "type": "required_beat_3_completed",
      "subjects": [
        "陆沉",
        "赵铁",
        "顾岚"
      ],
      "objects": [
        "废弃通勤车",
        "四名新临时受保护者",
        "四枚R-17应答片"
      ],
      "location": "旧通勤道",
      "status": "completed",
      "scene_index": 2,
      "source_tier": "chapter_event"
    },
    "chapter-0022-beat-004": {
      "event_id": "chapter-0022-beat-004",
      "type": "required_beat_4_completed",
      "subjects": [
        "陆沉",
        "赵铁",
        "顾岚"
      ],
      "objects": [],
      "location": "旧通勤道",
      "status": "completed",
      "scene_index": 2,
      "source_tier": "chapter_event"
    },
    "chapter-0022-beat-005": {
      "event_id": "chapter-0022-beat-005",
      "type": "required_beat_5_completed",
      "subjects": [
        "顾岚",
        "赵铁",
        "陆沉"
      ],
      "objects": [
        "消防救援车",
        "废弃通勤车",
        "救援车绞盘",
        "弹性筋腱束带"
      ],
      "location": "旧通勤道",
      "status": "completed",
      "scene_index": 3,
      "source_tier": "chapter_event"
    },
    "chapter-0022-beat-006": {
      "event_id": "chapter-0022-beat-006",
      "type": "required_beat_6_completed",
      "subjects": [
        "陆沉",
        "赵铁",
        "顾岚"
      ],
      "objects": [
        "四名新临时受保护者",
        "四枚R-17应答片",
        "消防救援车金属器材柜"
      ],
      "location": "旧通勤道",
      "status": "completed",
      "scene_index": 3,
      "source_tier": "chapter_event"
    },
    "chapter-0022-beat-007": {
      "event_id": "chapter-0022-beat-007",
      "type": "required_beat_7_completed",
      "subjects": [
        "四名新临时受保护者"
      ],
      "objects": [],
      "location": "消防站双门验伤区",
      "status": "completed",
      "scene_index": 4,
      "source_tier": "chapter_event"
    },
    "chapter-0022-beat-008": {
      "event_id": "chapter-0022-beat-008",
      "type": "required_beat_8_completed",
      "subjects": [
        "陆沉",
        "赵铁",
        "顾岚"
      ],
      "objects": [
        "消防救援车"
      ],
      "location": "城西物流园方向通勤道",
      "status": "completed",
      "scene_index": 4,
      "source_tier": "chapter_event"
    },
    "chapter-0023-beat-001": {
      "event_id": "chapter-0023-beat-001",
      "type": "required_beat_1_completed",
      "subjects": [
        "赵铁",
        "陆沉",
        "顾岚"
      ],
      "objects": [
        "消防救援车"
      ],
      "location": "物流园外围维修道",
      "status": "completed",
      "scene_index": 1,
      "source_tier": "chapter_event"
    },
    "chapter-0023-beat-002": {
      "event_id": "chapter-0023-beat-002",
      "type": "required_beat_2_completed",
      "subjects": [
        "赵铁",
        "陆沉",
        "顾岚"
      ],
      "objects": [
        "屏蔽箱",
        "泵车应答器"
      ],
      "location": "泵房检修门",
      "status": "completed",
      "scene_index": 1,
      "source_tier": "chapter_event"
    },
    "chapter-0023-beat-003": {
      "event_id": "chapter-0023-beat-003",
      "type": "required_beat_3_completed",
      "subjects": [
        "陆沉",
        "赵铁",
        "顾岚"
      ],
      "objects": [
        "两组消防站原配主滤芯"
      ],
      "location": "泵房检修门",
      "status": "completed",
      "scene_index": 2,
      "source_tier": "chapter_event"
    },
    "chapter-0023-beat-004": {
      "event_id": "chapter-0023-beat-004",
      "type": "required_beat_4_completed",
      "subjects": [
        "陆沉",
        "赵铁",
        "顾岚"
      ],
      "objects": [],
      "location": "泵房检修门",
      "status": "completed",
      "scene_index": 2,
      "source_tier": "chapter_event"
    },
    "chapter-0023-beat-005": {
      "event_id": "chapter-0023-beat-005",
      "type": "required_beat_5_completed",
      "subjects": [
        "韩野",
        "林夏",
        "苏晴"
      ],
      "objects": [],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 3,
      "source_tier": "chapter_event"
    },
    "chapter-0023-beat-006": {
      "event_id": "chapter-0023-beat-006",
      "type": "required_beat_6_completed",
      "subjects": [
        "顾岚"
      ],
      "objects": [],
      "location": "梁魁办公室外接点",
      "status": "completed",
      "scene_index": 3,
      "source_tier": "chapter_event"
    },
    "chapter-0023-beat-007": {
      "event_id": "chapter-0023-beat-007",
      "type": "required_beat_7_completed",
      "subjects": [
        "陆沉",
        "赵铁"
      ],
      "objects": [],
      "location": "泵房检修门",
      "status": "completed",
      "scene_index": 4,
      "source_tier": "chapter_event"
    },
    "chapter-0023-beat-008": {
      "event_id": "chapter-0023-beat-008",
      "type": "required_beat_8_completed",
      "subjects": [
        "陆沉",
        "赵铁",
        "顾岚"
      ],
      "objects": [],
      "location": "负二层冷库",
      "status": "completed",
      "scene_index": 4,
      "source_tier": "chapter_event"
    },
    "chapter-0024-beat-001": {
      "event_id": "chapter-0024-beat-001",
      "type": "required_beat_1_completed",
      "subjects": [
        "赵铁",
        "陆沉",
        "顾岚"
      ],
      "objects": [],
      "location": "负二层冷库",
      "status": "completed",
      "scene_index": 1,
      "source_tier": "chapter_event"
    },
    "chapter-0024-beat-002": {
      "event_id": "chapter-0024-beat-002",
      "type": "required_beat_2_completed",
      "subjects": [
        "赵铁",
        "陆沉",
        "顾岚"
      ],
      "objects": [],
      "location": "负二层冷库",
      "status": "completed",
      "scene_index": 1,
      "source_tier": "chapter_event"
    },
    "chapter-0024-beat-003": {
      "event_id": "chapter-0024-beat-003",
      "type": "required_beat_3_completed",
      "subjects": [
        "赵铁",
        "陆沉",
        "顾岚"
      ],
      "objects": [],
      "location": "无信号的装卸检修间",
      "status": "completed",
      "scene_index": 2,
      "source_tier": "chapter_event"
    },
    "chapter-0024-beat-004": {
      "event_id": "chapter-0024-beat-004",
      "type": "required_beat_4_completed",
      "subjects": [
        "赵铁",
        "陆沉",
        "顾岚"
      ],
      "objects": [],
      "location": "无信号的装卸检修间",
      "status": "completed",
      "scene_index": 2,
      "source_tier": "chapter_event"
    },
    "chapter-0024-beat-005": {
      "event_id": "chapter-0024-beat-005",
      "type": "required_beat_5_completed",
      "subjects": [
        "顾岚",
        "仓管员"
      ],
      "objects": [
        "纸质出库联",
        "离线备份"
      ],
      "location": "无信号的装卸检修间",
      "status": "completed",
      "scene_index": 3,
      "source_tier": "chapter_event"
    },
    "chapter-0024-beat-006": {
      "event_id": "chapter-0024-beat-006",
      "type": "required_beat_6_completed",
      "subjects": [
        "顾岚",
        "仓管员"
      ],
      "objects": [
        "纸质出库联",
        "离线备份"
      ],
      "location": "无信号的装卸检修间",
      "status": "completed",
      "scene_index": 3,
      "source_tier": "chapter_event"
    },
    "chapter-0024-beat-007": {
      "event_id": "chapter-0024-beat-007",
      "type": "required_beat_7_completed",
      "subjects": [
        "陆沉",
        "赵铁",
        "顾岚",
        "仓管员"
      ],
      "objects": [
        "八名获救者"
      ],
      "location": "物流园外围维修道",
      "status": "completed",
      "scene_index": 4,
      "source_tier": "chapter_event"
    },
    "chapter-0024-beat-008": {
      "event_id": "chapter-0024-beat-008",
      "type": "required_beat_8_completed",
      "subjects": [
        "顾岚"
      ],
      "objects": [],
      "location": "物流园外围维修道",
      "status": "completed",
      "scene_index": 4,
      "source_tier": "chapter_event"
    },
    "chapter-0025-beat-001": {
      "event_id": "chapter-0025-beat-001",
      "type": "required_beat_1_completed",
      "subjects": [
        "陆沉",
        "赵铁",
        "顾岚"
      ],
      "objects": [],
      "location": "物流园外围维修道",
      "status": "completed",
      "scene_index": 1,
      "source_tier": "chapter_event"
    },
    "chapter-0025-beat-002": {
      "event_id": "chapter-0025-beat-002",
      "type": "required_beat_2_completed",
      "subjects": [
        "陆沉",
        "赵铁",
        "顾岚"
      ],
      "objects": [],
      "location": "物流园外围维修道",
      "status": "completed",
      "scene_index": 1,
      "source_tier": "chapter_event"
    },
    "chapter-0025-beat-003": {
      "event_id": "chapter-0025-beat-003",
      "type": "required_beat_3_completed",
      "subjects": [
        "苏晴",
        "林夏"
      ],
      "objects": [],
      "location": "消防站一层车库区",
      "status": "completed",
      "scene_index": 2,
      "source_tier": "chapter_event"
    },
    "chapter-0025-beat-004": {
      "event_id": "chapter-0025-beat-004",
      "type": "required_beat_4_completed",
      "subjects": [
        "赵铁",
        "陆沉",
        "顾岚"
      ],
      "objects": [],
      "location": "物流园外围维修道",
      "status": "completed",
      "scene_index": 2,
      "source_tier": "chapter_event"
    },
    "chapter-0025-beat-005": {
      "event_id": "chapter-0025-beat-005",
      "type": "required_beat_5_completed",
      "subjects": [
        "陆沉",
        "赵铁",
        "顾岚"
      ],
      "objects": [
        "M-17-04主滤芯",
        "M-17-05主滤芯"
      ],
      "location": "物流园外围维修道",
      "status": "completed",
      "scene_index": 3,
      "source_tier": "chapter_event"
    },
    "chapter-0025-beat-006": {
      "event_id": "chapter-0025-beat-006",
      "type": "required_beat_6_completed",
      "subjects": [
        "顾岚"
      ],
      "objects": [
        "黑旗名册"
      ],
      "location": "物流园外围维修道",
      "status": "completed",
      "scene_index": 3,
      "source_tier": "chapter_event"
    },
    "chapter-0025-beat-007": {
      "event_id": "chapter-0025-beat-007",
      "type": "required_beat_7_completed",
      "subjects": [],
      "objects": [],
      "location": "物流园外围维修道",
      "status": "completed",
      "scene_index": 4,
      "source_tier": "chapter_event"
    },
    "chapter-0025-beat-008": {
      "event_id": "chapter-0025-beat-008",
      "type": "required_beat_8_completed",
      "subjects": [
        "赵铁",
        "顾岚",
        "陆沉"
      ],
      "objects": [],
      "location": "物流园外围维修道",
      "status": "completed",
      "scene_index": 4,
      "source_tier": "chapter_event"
    }
  }
}

# Characters
{
  "character-bc336fab9f77fde2": {
    "name": "陆沉",
    "status": "健康，保持警戒",
    "location": "地下商超监控室"
  }
}

# Timeline
[
  {
    "id": "ev-001-blackout",
    "chapter": 1,
    "location": "地下商超",
    "text": "城市断电，商超备用电源启动"
  },
  {
    "id": "ev-001-first-bite",
    "chapter": 1,
    "location": "地下一层营业区",
    "text": "陆沉确认人群中出现感染者"
  },
  {
    "chapter_index": 23,
    "events": [
      {
        "text": "顾岚用镜片反光扫过墙角，发现一根被重新接过的信号线，线头绕向后廊，却没有人守在明处。"
      }
    ],
    "id": "chapter-000023",
    "summary": "消防救援车从安全交汇点拐入城西物流园外围的维修道时，四名新临时受保护者已经交给接应车，沿另一条路线返回消防站双门验伤区。 车轮压过碎石，车内只剩陆沉、赵铁和顾岚三人。",
    "world_changes": []
  },
  {
    "chapter_index": 24,
    "events": [
      {
        "text": "赵铁把八人的人数、伤势和发现位置记进纸条，塞入防水袋。"
      }
    ],
    "id": "chapter-000024",
    "summary": "十分钟倒计时还剩八分四十七秒。 负二层冷库外，北院方向传来叉车倒车的蜂鸣，隔着厚墙也能听见主滤芯箱被推上车板的沉闷撞击。",
    "world_changes": [
      {
        "text": "Infection pressure is active in the chapter.",
        "type": "infection_pressure"
      }
    ]
  },
  {
    "chapter_index": 25,
    "events": [
      {
        "text": "真正的车，必须在下一次转向之前留下来。"
      },
      {
        "text": "完好的这一组是M-17-04，裂开的也是M-17-05，但必须送回去待检，不能算完好备用。"
      },
      {
        "text": "屋顶主控还在蓄能，下一轮启动前必须有人把它拆掉。"
      }
    ],
    "id": "chapter-000025",
    "summary": "在物流园外围维修道，北院方向的发动机声刚从雾里传来，陆沉便回头看了一眼那辆被弃在路边的消防救援车。 八名刚救出的获救者已经转移到维修道内侧，没有人被允许参加追车；仓管员带着两名自愿留下的非战斗人员守在他们身边，按苏晴交代逐人看护，伤者的转移和验伤都不因外面的追截而中断。",
    "world_changes": []
  }
]

# Constraints
[
  {
    "id": "constraint-71312525873d00e2",
    "content": "营业区侧门：感染者曾撞开的侧门，已被机械扣与倒塌货架封死，禁止再次通行。",
    "status": "active"
  },
  {
    "id": "constraint-32147763064b392f",
    "content": "三扇门不是同一扇门；人物移动时必须明确门名。",
    "status": "active"
  },
  {
    "id": "constraint-004addece2519f28",
    "content": "检修门外防撞柱：紧贴东侧检修门外墙的两根钢柱，与北侧卸货区维修坑相隔一段车道；适合用钢索将贴门目标锁死在门侧，不能与维修坑混写成同一位置。",
    "status": "active"
  },
  {
    "id": "constraint-2559400a9a200744",
    "content": "提取必须由陆沉直接接触有效击杀后的目标或与目标相连的武器；向装备、门或设施转移时，陆沉必须同时维持“尸体—武器/钢索—自身—承载物”的连续接触链，且目标在三米内。接触链中断后本次词条不能远距离飞向设施。",
    "status": "active"
  }
]

# Runtime Memory Metadata
{
  "item_count": 19,
  "source": "notion-api",
  "status": "ready"
}

# Memory Index
{
  "source": "notion-api",
  "status": "ready",
  "item_count": 19,
  "items": [
    {
      "type": "timeline_event",
      "name": "provider_smoke",
      "id": "provider_smoke:20260831T064401413146Z"
    },
    {
      "type": "timeline_event",
      "name": "provider_smoke",
      "id": "provider_smoke:20260831T055532911216Z"
    },
    {
      "type": "timeline_event",
      "name": "provider_smoke",
      "id": "provider_smoke:20260831T013731058622Z"
    },
    {
      "type": "timeline_event",
      "name": "provider_smoke",
      "id": "provider_smoke:20260702T042322101389Z"
    },
    {
      "type": "timeline_event",
      "name": "provider_smoke",
      "id": "provider_smoke:20260702T042002576075Z"
    },
    {
      "type": "timeline_event",
      "name": "provider_smoke",
      "id": "provider_smoke:20260702T041903430964Z"
    },
    {
      "type": "timeline_event",
      "name": "provider_smoke",
      "id": "provider_smoke:20260702T012804196651Z"
    },
    {
      "type": "timeline_event",
      "name": "provider_smoke",
      "id": "provider_smoke:20260702T012606318929Z"
    },
    {
      "type": "story_state",
      "name": "current_outline",
      "id": "story_project:current_outline"
    },
    {
      "type": "story_state",
      "name": "tracking:上下文.md",
      "id": "story_project:tracking:上下文.md"
    },
    {
      "type": "story_state",
      "name": "tracking:伏笔.md",
      "id": "story_project:tracking:伏笔.md"
    },
    {
      "type": "story_state",
      "name": "tracking:时间线.md",
      "id": "story_project:tracking:时间线.md"
    },
    {
      "type": "story_state",
      "name": "tracking:角色状态.md",
      "id": "story_project:tracking:角色状态.md"
    },
    {
      "type": "world_state",
      "name": "setting:作品设定.md",
      "id": "story_project:setting:作品设定.md"
    },
    {
      "type": "world_state",
      "name": "setting:地点/临江市地下商超.md",
      "id": "story_project:setting:地点/临江市地下商超.md"
    },
    {
      "type": "world_state",
      "name": "setting:能力体系.md",
      "id": "story_project:setting:能力体系.md"
    },
    {
      "type": "world_state",
      "name": "setting:角色/苏晴.md",
      "id": "story_project:setting:角色/苏晴.md"
    },
    {
      "type": "world_state",
      "name": "setting:角色/赵铁.md",
      "id": "story_project:setting:角色/赵铁.md"
    },
    {
      "type": "world_state",
      "name": "setting:角色/陆沉.md",
      "id": "story_project:setting:角色/陆沉.md"
    }
  ],
  "source_mappings": [
    {
      "index": 0,
      "source": "notion-api",
      "memory_id": "provider_smoke:20260831T064401413146Z",
      "type": "timeline_event",
      "name": "provider_smoke",
      "page_id": "3cd7975e-f1a1-81c7-8b5f-cae831f76858",
      "page_url": "https://app.notion.com/p/provider_smoke-3cd7975ef1a181c78b5fcae831f76858",
      "page_index": 0
    },
    {
      "index": 1,
      "source": "notion-api",
      "memory_id": "provider_smoke:20260831T055532911216Z",
      "type": "timeline_event",
      "name": "provider_smoke",
      "page_id": "3cd7975e-f1a1-813f-b911-ffdc66b805c5",
      "page_url": "https://app.notion.com/p/provider_smoke-3cd7975ef1a1813fb911ffdc66b805c5",
      "page_index": 1
    },
    {
      "index": 2,
      "source": "notion-api",
      "memory_id": "provider_smoke:20260831T013731058622Z",
      "type": "timeline_event",
      "name": "provider_smoke",
      "page_id": "3cd7975e-f1a1-8152-a670-eff9499e1033",
      "page_url": "https://app.notion.com/p/provider_smoke-3cd7975ef1a18152a670eff9499e1033",
      "page_index": 2
    },
    {
      "index": 3,
      "source": "notion-api",
      "memory_id": "provider_smoke:20260702T042322101389Z",
      "type": "timeline_event",
      "name": "provider_smoke",
      "page_id": "3917975e-f1a1-81e0-80ea-e2bb98975e58",
      "page_url": "https://app.notion.com/p/provider_smoke-3917975ef1a181e080eae2bb98975e58",
      "page_index": 3
    },
    {
      "index": 4,
      "source": "notion-api",
      "memory_id": "provider_smoke:20260702T042002576075Z",
      "type": "timeline_event",
      "name": "provider_smoke",
      "page_id": "3917975e-f1a1-81bc-bfa6-c425abc1f5f1",
      "page_url": "https://app.notion.com/p/provider_smoke-3917975ef1a181bcbfa6c425abc1f5f1",
      "page_index": 4
    },
    {
      "index": 5,
      "source": "notion-api",
      "memory_id": "provider_smoke:20260702T041903430964Z",
      "type": "timeline_event",
      "name": "provider_smoke",
      "page_id": "3917975e-f1a1-8164-81fc-f5d8599112c6",
      "page_url": "https://app.notion.com/p/provider_smoke-3917975ef1a1816481fcf5d8599112c6",
      "page_index": 5
    },
    {
      "index": 6,
      "source": "notion-api",
      "memory_id": "provider_smoke:20260702T012804196651Z",
      "type": "timeline_event",
      "name": "provider_smoke",
      "page_id": "3917975e-f1a1-8126-88d0-d865d7550dbf",
      "page_url": "https://app.notion.com/p/provider_smoke-3917975ef1a1812688d0d865d7550dbf",
      "page_index": 6
    },
    {
      "index": 7,
      "source": "notion-api",
      "memory_id": "provider_smoke:20260702T012606318929Z",
      "type": "timeline_event",
      "name": "provider_smoke",
      "page_id": "3917975e-f1a1-810a-a088-c35204382587",
      "page_url": "https://app.notion.com/p/provider_smoke-3917975ef1a1810aa088c35204382587",
      "page_index": 7
    },
    {
      "index": 8,
      "source": "story_project",
      "memory_id": "story_project:current_outline",
      "type": "story_state",
      "name": "current_outline",
      "path": "D:\\Pycharm\\PythonProject\\NovelAgent\\books\\尸潮之下_我能提取进化词条_重写V2\\大纲\\细纲_第026章.md"
    },
    {
      "index": 9,
      "source": "story_project",
      "memory_id": "story_project:tracking:上下文.md",
      "type": "story_state",
      "name": "tracking:上下文.md",
      "path": "D:\\Pycharm\\PythonProject\\NovelAgent\\books\\尸潮之下_我能提取进化词条_重写V2\\追踪\\上下文.md"
    },
    {
      "index": 10,
      "source": "story_project",
      "memory_id": "story_project:tracking:伏笔.md",
      "type": "story_state",
      "name": "tracking:伏笔.md",
      "path": "D:\\Pycharm\\PythonProject\\NovelAgent\\books\\尸潮之下_我能提取进化词条_重写V2\\追踪\\伏笔.md"
    },
    {
      "index": 11,
      "source": "story_project",
      "memory_id": "story_project:tracking:时间线.md",
      "type": "story_state",
      "name": "tracking:时间线.md",
      "path": "D:\\Pycharm\\PythonProject\\NovelAgent\\books\\尸潮之下_我能提取进化词条_重写V2\\追踪\\时间线.md"
    },
    {
      "index": 12,
      "source": "story_project",
      "memory_id": "story_project:tracking:角色状态.md",
      "type": "story_state",
      "name": "tracking:角色状态.md",
      "path": "D:\\Pycharm\\PythonProject\\NovelAgent\\books\\尸潮之下_我能提取进化词条_重写V2\\追踪\\角色状态.md"
    },
    {
      "index": 13,
      "source": "story_project",
      "memory_id": "story_project:setting:作品设定.md",
      "type": "world_state",
      "name": "setting:作品设定.md",
      "path": "D:\\Pycharm\\PythonProject\\NovelAgent\\books\\尸潮之下_我能提取进化词条_重写V2\\设定\\作品设定.md"
    },
    {
      "index": 14,
      "source": "story_project",
      "memory_id": "story_project:setting:地点/临江市地下商超.md",
      "type": "world_state",
      "name": "setting:地点/临江市地下商超.md",
      "path": "D:\\Pycharm\\PythonProject\\NovelAgent\\books\\尸潮之下_我能提取进化词条_重写V2\\设定\\地点\\临江市地下商超.md"
    },
    {
      "index": 15,
      "source": "story_project",
      "memory_id": "story_project:setting:能力体系.md",
      "type": "world_state",
      "name": "setting:能力体系.md",
      "path": "D:\\Pycharm\\PythonProject\\NovelAgent\\books\\尸潮之下_我能提取进化词条_重写V2\\设定\\能力体系.md"
    },
    {
      "index": 16,
      "source": "story_project",
      "memory_id": "story_project:setting:角色/苏晴.md",
      "type": "world_state",
      "name": "setting:角色/苏晴.md",
      "path": "D:\\Pycharm\\PythonProject\\NovelAgent\\books\\尸潮之下_我能提取进化词条_重写V2\\设定\\角色\\苏晴.md"
    },
    {
      "index": 17,
      "source": "story_project",
      "memory_id": "story_project:setting:角色/赵铁.md",
      "type": "world_state",
      "name": "setting:角色/赵铁.md",
      "path": "D:\\Pycharm\\PythonProject\\NovelAgent\\books\\尸潮之下_我能提取进化词条_重写V2\\设定\\角色\\赵铁.md"
    },
    {
      "index": 18,
      "source": "story_project",
      "memory_id": "story_project:setting:角色/陆沉.md",
      "type": "world_state",
      "name": "setting:角色/陆沉.md",
      "path": "D:\\Pycharm\\PythonProject\\NovelAgent\\books\\尸潮之下_我能提取进化词条_重写V2\\设定\\角色\\陆沉.md"
    }
  ],
  "last_run": {
    "id": "chapter_25_20260901T040407553632Z",
    "status": "committed",
    "committed": true,
    "chapter_index": 25,
    "goal": "recover_from_rejected_run",
    "workflow": [
      "build_snapshot",
      "pre_validate_bridge",
      "generate_chapter",
      "validate",
      "repair_if_needed",
      "commit_snapshot"
    ],
    "problem_codes": [],
    "problem_count": 0,
    "blocking_problem_count": 0,
    "warning_count": 0,
    "severity_counts": [
      {
        "count": 0,
        "severity": "critical"
      },
      {
        "count": 0,
        "severity": "high"
      },
      {
        "count": 0,
        "severity": "medium"
      },
      {
        "count": 0,
        "severity": "low"
      }
    ],
    "requested_focus": [
      "continuity",
      "spatial",
      "logic"
    ],
    "executed_checks": [
      "continuity",
      "spatial",
      "logic",
      "story_project",
      "llm"
    ],
    "skipped_checks": [],
    "problem_evidence": [],
    "repair_attempts": 0,
    "repair_plan": null,
    "repair_evidence": [],
    "repair_deltas": [],
    "repair_effective": null,
    "repair_stalled": false,
    "repair_introduced_new_problems": false,
    "error_type": null,
    "error_message": null,
    "director_mode": "rule",
    "director_status": "completed",
    "director_model_call": null
  },
  "snapshot_builder_audit": {
    "source": "notion-api",
    "status": "ready",
    "item_count": 19,
    "applied_count": 19,
    "skipped_count": 0,
    "deduplicated_count": 0,
    "skipped_reason_counts": [],
    "skipped_severity_counts": []
  }
}

# Recovery Context
{
  "available": false
}


# StoryProject Chapter Blueprint
{
  "chapter_blueprint": {
    "chapter_index": 26,
    "outline_path": "D:\\Pycharm\\PythonProject\\NovelAgent\\books\\尸潮之下_我能提取进化词条_重写V2\\大纲\\细纲_第026章.md",
    "title": "第26章 主控塔熄灯",
    "core_event": "外勤组在带走两组主滤芯和八名获救者前摧毁物流园清扫主控，并从与主控共生的脉冲变异体上提取蓝色设施词条“反相耳膜”，赋予便携消防扩音器，为最终尸潮留下九十秒断链手段。",
    "required_beats": [
      {
        "index": 1,
        "text": "开篇承接梁魁指挥车东逃与屋顶主控蓄能；赵铁安排获救者和滤芯先进入救援车防护区，顾岚与陆沉沿明确的外梯上屋顶，不能把人员、滤芯和战斗位置混在一起。"
      },
      {
        "index": 2,
        "text": "屋顶主控已与一只吸收广播脉冲的变异体“回声囊”共生，普通断电会触发储能释放；赵铁从楼下远程读电流，顾岚切断备用天线，陆沉用弱点视界定位囊体与电容之间的主核。"
      },
      {
        "index": 3,
        "text": "击杀必须依靠三人配合：赵铁制造反向负载、顾岚在蓄能间隙打断天线支撑、陆沉只在核心暴露时完成有效击杀；弱点视界的精神消耗继续累积，不能写成无代价连用。"
      },
      {
        "index": 4,
        "text": "回声囊掉落蓝色设施词条“反相耳膜”；陆沉在尸体、武器、自身和三米内便携消防扩音器之间维持连续接触链完成赋予，不装入人体，侵蚀值保持6/100。"
      },
      {
        "index": 5,
        "text": "反相耳膜只能对已记录的一种R-17引导频率制造九十秒反相窗口；启用时覆盖范围内友方无线通信也会中断，扩音器过热后十二小时不可再用，必须预先计时协同。"
      },
      {
        "index": 6,
        "text": "物流园主控被物理断开并保留残骸取证，后续不能再重复发出自动清扫脉冲；但第一轮脉冲已经让万级尸潮取得东街方向，摧毁主控不能让尸潮凭空消失。"
      },
      {
        "index": 7,
        "text": "两组主滤芯分别封存：完好组用于首次安装，破损组只作为待修备用；赵铁完成机械与爆炸物检查后，以消防救援车和缴获的真滤芯运输车组成两车撤离队，八名获救者分乘两车，赵铁与顾岚分别驾驶、陆沉押后照看伤员，不能把十一人和两组滤芯强塞进一辆车，也不能无过渡返回消防站。"
      },
      {
        "index": 8,
        "text": "梁魁仍持便携控制器且本人未受制，他正以三名活体信标开路；林夏要求外勤组优先把滤芯送回，不得为了个人复仇改变任务。"
      }
    ],
    "ending_pressure": "物流园主控熄灭后，节点四倒计时仍继续；消防站画面里，梁魁的指挥车已经出现在东街尽头，车后是第一道尸潮黑线，倒计时剩余三小时五十五分。",
    "chapter_context_read_set": null,
    "source_path": "D:\\Pycharm\\PythonProject\\NovelAgent\\books\\尸潮之下_我能提取进化词条_重写V2\\大纲\\细纲_第026章.md",
    "missing_fields": []
  },
  "read_set_context_digest": "7b984486610c672ed4d53ba6de6524aadc3fdece6cb67d4f8fa39ccdbde90b19",
  "outline_source": {
    "relative_path": "大纲/细纲_第026章.md",
    "sha256": "63edfecbe042111020903a8653b66d6eb6da1039e11c42fc0bd4e46050464377",
    "original_chars": 830,
    "excerpt": "# 第26章 主控塔熄灯\n\n[…完整条目已省略…]\n\n## 必写节拍\n\n- 开篇承接梁魁指挥车东逃与屋顶主控蓄能；赵铁安排获救者和滤芯先进入救援车防护区，顾岚与陆沉沿明确的外梯上屋顶，不能把人员、滤芯和战斗位置混在一起。\n- 屋顶主控已与一只吸收广播脉冲的变异体“回声囊”共生，普通断电会触发储能释放；赵铁从楼下远程读电流，顾岚切断备用天线，陆沉用弱点视界定位囊体与电容之间的主核。\n- 击杀必须依靠三人配合：赵铁制造反向负载、顾岚在蓄能间隙打断天线支撑、陆沉只在核心暴露时完成有效击杀；弱点视界的精神消耗继续累积，不能写成无代价连用。\n- 回声囊掉落蓝色设施词条“反相耳膜”；陆沉在尸体、武器、自身和三米内便携消防扩音器之间维持连续接触链完成赋予，不装入人体，侵蚀值保持6/100。\n- 反相耳膜只能对已记录的一种R-17引导频率制造九十秒反相窗口；启用时覆盖范围内友方无线通信也会中断，扩音器过热后十二小时不可再用，必须预先计时协同。\n- 物流园主控被物理断开并保留残骸取证，后续不能再重复发出自动清扫脉冲；但第一轮脉冲已经让万级尸潮取得东街方向，摧毁主控不能让尸潮凭空消失。\n- 两组主滤芯分别封存：完好组用于首次安装，破损组只作为待修备用；赵铁完成机械与爆炸物检查后，以消防救援车和缴获的真滤芯运输车组成两车撤离队，八名获救者分乘两车，赵铁与顾岚分别驾驶、陆沉押后照看伤员，不能把十一人和两组滤芯强塞进一辆车，也不能无过渡返回消防站。\n- 梁魁仍持便携控制器且本人未受制，他正以三名活体信标开路；林夏要求外勤组优先把滤芯送回，不得为了个人复仇改变任务。\n\n结尾压力：物流园主控熄灭后，节点四倒计时仍继续；消防站画面里，梁魁的指挥车已经出现在东街尽头，车后是第一道尸潮黑线，倒计时剩余三小时五十五分。",
    "source_selection": {
      "schema_version": "1.0",
      "policy": "story_project_source_paragraph_relevance_v1",
      "source_sha256": "63edfecbe042111020903a8653b66d6eb6da1039e11c42fc0bd4e46050464377",
      "original_chars": 830,
      "selected_items": [
        {
          "id": "document",
          "sha256": "63edfecbe042111020903a8653b66d6eb6da1039e11c42fc0bd4e46050464377",
          "original_chars": 830,
          "start_char": 0,
          "end_char": 830
        }
      ],
      "omitted_count": 0
    },
    "selection": {
      "schema_version": "1.0",
      "policy": "input_pack_story_source_paragraph_relevance_v1",
      "source_sha256": "63edfecbe042111020903a8653b66d6eb6da1039e11c42fc0bd4e46050464377",
      "original_chars": 830,
      "selected_items": [
        {
          "id": "paragraph:0",
          "sha256": "283d2addd8a4d27ddd6df467801e7820ab61384f3cfd16c3ea7dee6b08bbd014",
          "original_chars": 12,
          "start_char": 0,
          "end_char": 12
        },
        {
          "id": "paragraph:2",
          "sha256": "7c49a914ede38e29b597ae3a599940d4d78e1b0fb2639d95012329e06025da72",
          "original_chars": 7,
          "start_char": 103,
          "end_char": 110
        },
        {
          "id": "paragraph:3",
          "sha256": "9831f1ce21e42524f8037f988e46c8dcb8548444fad7ab42ed8f4c6e8836bfb0",
          "original_chars": 644,
          "start_char": 112,
          "end_char": 756
        },
        {
          "id": "paragraph:4",
          "sha256": "446a43f2c3e5b2854a54cfdfb7a038236738843ef671146ee82b8398b6d5f834",
          "original_chars": 71,
          "start_char": 758,
          "end_char": 829
        }
      ],
      "omitted_count": 1
    },
    "truncated": true
  },
  "previous_chapter_context": {
    "schema_version": "1.0",
    "chapter_index": 25,
    "source_kind": "story_project_prose",
    "path_ref": {
      "root_id": "story_project",
      "relative_path": "正文/第025章_第25章 黑旗名册.md"
    },
    "sha256": "6de27e2d49d089daa662f7bae73608d6e80d8697fbe148ba09785d5f1a818789",
    "original_chars": 3216,
    "generation_excerpt": {
      "text": "在物流园外围维修道，北院方向的发动机声刚从雾里传来，陆沉便回头看了一眼那辆被弃在路边的消防救援车。八名刚救出的获救者已经转移到维修道内侧，没有人被允许参加追车；仓管员带着两名自愿留下的非战斗人员守在他们身边，按苏晴交代逐人看护，伤者的转移和验伤都不因外面的追截而中断。仓管员把通往北院的装卸通道岔口指给赵铁，随后留在原地继续照看获救者。陆沉、赵铁、顾岚三人沿着堆货墙后的通道快步追向北院，脚下是被雨水冲亮的铁轨和断裂的托盘。\n\n北院广播突然接通，梁魁的声音经过变声器放大，像一把钝刀刮过仓库顶棚：“核心换设备，滤芯车出库，重复一遍，核心换设备。”两辆外观相同的运输车一前一后驶出阴影，车厢涂着同样的灰漆，连车尾的红色警示灯都在同一节拍上闪烁。陆沉按住耳机，向消防站报出车牌残号和出库时间，要求联合值勤组远程复核。\n\n几秒后，韩野的声音切进频道：“复核不通过。出库联、原配序列和当前任务不一致，消防站拒绝交易。”林夏紧接着确认：“任何人不得拿R-17铅盒换人，也不得拿它换滤芯。铅盒仍在消防站独立隔离柜，放弃阈值不变。”陆沉看着两辆车压过积水，手指从扳机上移开。他们追的是滤芯，不是要用一件未知核心去赌一群人的命。\n\n梁魁像是听见了这声拒绝，广播里传出短促的笑声。前车忽然加速，后车却故意横摆，两束车灯同时扫向三人，远处几名持枪的黑旗成员从货架间探出身影。腐味屏蔽剂被风卷散，尸群没有被完全引开，铁门外立刻响起杂乱的抓挠声。顾岚贴住转角，抬枪封住北侧出口；赵铁压低身体，死死盯着两辆车的轮距；陆沉则绕向普通尸群涌来的侧面，三人把追截线一点点收紧。真正的车，必须在下一次转向之前留下来。\n\n陆沉刚绕过尸群侧翼，耳机里突然切进苏晴的声音，压得很低，却没有半点迟疑：“把名册照片放大，第三列的前缀是LJ-赤潮-04。”\n\n赵铁抬眼看向迎面冲来的两辆车。它们的车厢、车灯和伪装泥点几乎一模一样，连后厢门上的划痕都像是被刻意复制出来的。陆沉耳机里传来林夏的确认：“照片已经转回消防站一层车库区，苏晴要求按风险公开条款直接通报。”\n\n短暂的电流杂音后，苏晴自己接上了频道。\n\n“我在爆发前接收过这批早期临床样本。”她说，“我只负责接收、登记和转交，编码前缀我认得，但我不知道完整的实验目的，也没有参与后续处置。这个事实可以核验，不能替我洗清，也不能直接给我定罪。”\n\n频道里静了两秒。陆沉没有追问，赵铁也没有回头。前车趁着尸群扑向铁门的空隙猛打方向，后车则突然横过来，枪口从车窗里探出。顾岚贴着转角开火，子弹打碎后车外壳上的假编号，逼得它向南偏移。\n\n“不是前车！”赵铁忽然吼道。\n\n他盯住两辆车的底盘，在交错的车灯里比出一个手势：“消防站原配滤芯序列的接口磨损在右侧，前车的磨损方向对不上。还有重量——前车起步太轻，后车压得悬挂下沉，真正的主滤芯在后面！”\n\n顾岚立刻将一枚震爆罐掷进转向口，爆响封住南侧的维修岔道。后车司机猛踩刹车，车轮擦着水泥地甩出一串火星。与此同时，陆沉冲进普通尸群的侧面，借尸体撞击车头的一瞬扣住一具歪斜的铁架，狠狠拖倒在路中央。真车退路被截断，驾驶室里的黑旗枪手推门探出，腐味屏蔽剂的气味混在风里，却没能骗过他的视线。\n\n枪声贴着陆沉耳侧掠过。他伏低身体翻到车尾，顾岚从侧面压住射界，赵铁则扑向后厢的锁扣。车内变异体撞得厢板咚咚作响，外面的尸群也被枪声重新吸引过来。三人没有贸然打开车厢，只用钢索先锁住后轴，把发动机彻底逼停。\n\n耳机里，苏晴的声音仍在发颤，却清晰地报出那串前缀：“LJ-赤潮-04之后，主滤芯序列会接续到M-17。别信车门上的批次号，看接口内圈。”\n\n赵铁撬开观察盖，灯光扫过内圈的磨损，抬头与陆沉对视。真正的车，已经被他们截在维修道中央。\n\n赵铁把观察盖彻底掀开，先用消防站原配序列核对接口，再摸过内圈那道被反复插拔留下的磨损痕。车厢里传出沉闷的金属碰撞声，他示意陆沉和顾岚退开，撬棍从侧缝探进去，压下车底的锁扣。\n\n“外壳批次能伪造，接口磨损伪造不了。”赵铁咬住手电，抬起电子秤复核运输重量，“这辆的载重与两组主滤芯和防震架的合计重量相符；前车起步太轻，里面才是配重和空壳。”\n\n维修道尽头，顾岚已经把两辆废弃货车横过来，封死了通往南侧转向口的路。陆沉没有再启用弱点视界，只抄起路边的钢管，一脚踢翻滚来的油桶。腐味屏蔽剂混着尸臭漫开，普通尸群被气味牵引着撞向车尾，却骗不过黑旗枪手。车顶的射孔立刻喷出火舌，一名枪手朝陆沉连续点射，另一个黑影则从车厢侧面扑下，四肢落地时带起一片不正常的骨响。\n\n顾岚压低身子，连开两枪逼退枪手；赵铁趁着车门偏转，将高压撬杆插进轮轴。陆沉借尸群撞击的间隙绕到侧后方，一钢管砸断变异体的前肢，再用绞索勒住它的颈骨。赵铁猛拧阀门，车底锁扣终于松开。三人合力拖出第一只铅封箱，第二只箱子的外壳却在碰撞中裂开，细碎的保温层从缝里簌簌掉落。\n\n“先封，不拆。”赵铁按住顾岚伸向卡扣的手，“编号只能确认到外壳。完好的这一组是M-17-04，裂开的也是M-17-05，但必须送回去待检，不能算完好备用。”\n\n耳机里沉默了两秒，林夏接过通话：“物流园现场不宣布恢复净水。名册、在场证言、控制器记录，全部按公开事实登记；至于谁在背后统筹，单列为推测，不许写成结论。”\n\n顾岚从驾驶座下摸出一本黑皮薄册，纸页边缘沾着暗红色的污迹。名册上密密麻麻列着多个地点，后面统一盖着“已交付”，另有几行被粗笔圈出，标注着“清扫优先级”，却没有组织名称、人员总表，甚至没有一张能拼出全貌的路线图。苏晴的声音再次插入频道，报出与早期临床样本相同的编码前缀。林夏当场复述她的证言：苏晴在爆发前只负责接收和登记样本，不知道完整的实验目的。那句话没有替她洗清嫌疑，也没有替任何人定罪，却让M-17序列和名册上的交付批次第一次彼此咬合。\n\n赵铁用钢带把两组滤芯分别固定，给裂壳箱贴上红色的待检封条。屋顶方向忽然传来低沉的蓄能声，终端倒计时跳成四小时四十五分。三人抬头时，远处一辆指挥车正调头驶向东街，车顶的黑旗在夜风里骤然绷直。\n\n那辆指挥车没有追向北院，反而猛地切进东街。梁魁显然已经放弃了主滤芯，车厢后门敞着，便携清扫控制器被人用绳带固定在车尾，几名黑旗枪手护在两侧。车头前方绑着三个人，手腕和脚踝都被反缚在保险杠支架上，身上各挂着一枚闪烁的信标。车轮每颠一下，三人的身体就被拖得撞向车架，微弱的挣扎在夜色里断断续续。\n\n“那是黑旗自己的人。”顾岚压低声音，望着其中一人胸口撕开的黑旗标记，“他们反对梁魁，所以被当成了新的活体信标。”\n\n陆沉已经抬起枪，却没有扣下扳机。车顶黑旗转向消防站——梁魁想用那三个人引开沿线尸群，也想逼站里的人开门。贸然追上去，只会让活体信标先死在他们眼前。\n\n“别追。”陆沉收枪，“先保住滤芯，再断主控。”\n\n赵铁应了一声，俯身检查刚夺下的两只箱体。他把钢带重新绕过车厢内的固定环，又在完好封装的箱角压上消防站原配铅封；另一只裂壳箱则加了两道横向束带，红色待检封条没有撕动半分。“两组都还在，编号分别是M-17-04和M-17-05。裂壳这一组不能接入净水线，运输震裂的位置还得回站里拆检。”\n\n顾岚展开纸质出库联，把物流园外围维修道到外围维修道北侧通道的路线逐段对照。维修道内侧的八名获救者仍由仓管员和两名志愿留下的非战斗人员看护，没有人被卷入追车；装卸通道也没有出现追车队伍。她确认完转移路线，才把视线投向屋顶：“人能按原路撤，车和滤芯却不能一起走。屋顶主控还在蓄能，下一轮启动前必须有人把它拆掉。”\n\n陆沉看向东街尽头。指挥车已经抢先驶向消防站，车前三枚信标在雾里一明一灭。杀死梁魁不是眼下的首要目标，真正逼近的，是屋顶那台仍在运行的物流园主控。赵铁留下固定两组滤芯，顾岚负责盯住撤离路线，陆沉则抬头确认通往屋顶的维修梯。三人没有追车，而是转身朝主控所在的楼顶准备推进。\n",
      "policy": "previous_chapter_10_90",
      "ranges": [
        {
          "start_char": 0,
          "end_char": 3216
        }
      ],
      "estimated_tokens": 804,
      "truncated": false,
      "source_sha256": "6de27e2d49d089daa662f7bae73608d6e80d8697fbe148ba09785d5f1a818789",
      "original_chars": 3216,
      "selected_items": [
        {
          "id": "document",
          "sha256": "6de27e2d49d089daa662f7bae73608d6e80d8697fbe148ba09785d5f1a818789",
          "original_chars": 3216,
          "start_char": 0,
          "end_char": 3216
        }
      ],
      "omitted_count": 0
    },
    "review_tail": {
      "text": "在物流园外围维修道，北院方向的发动机声刚从雾里传来，陆沉便回头看了一眼那辆被弃在路边的消防救援车。八名刚救出的获救者已经转移到维修道内侧，没有人被允许参加追车；仓管员带着两名自愿留下的非战斗人员守在他们身边，按苏晴交代逐人看护，伤者的转移和验伤都不因外面的追截而中断。仓管员把通往北院的装卸通道岔口指给赵铁，随后留在原地继续照看获救者。陆沉、赵铁、顾岚三人沿着堆货墙后的通道快步追向北院，脚下是被雨水冲亮的铁轨和断裂的托盘。\n\n北院广播突然接通，梁魁的声音经过变声器放大，像一把钝刀刮过仓库顶棚：“核心换设备，滤芯车出库，重复一遍，核心换设备。”两辆外观相同的运输车一前一后驶出阴影，车厢涂着同样的灰漆，连车尾的红色警示灯都在同一节拍上闪烁。陆沉按住耳机，向消防站报出车牌残号和出库时间，要求联合值勤组远程复核。\n\n几秒后，韩野的声音切进频道：“复核不通过。出库联、原配序列和当前任务不一致，消防站拒绝交易。”林夏紧接着确认：“任何人不得拿R-17铅盒换人，也不得拿它换滤芯。铅盒仍在消防站独立隔离柜，放弃阈值不变。”陆沉看着两辆车压过积水，手指从扳机上移开。他们追的是滤芯，不是要用一件未知核心去赌一群人的命。\n\n梁魁像是听见了这声拒绝，广播里传出短促的笑声。前车忽然加速，后车却故意横摆，两束车灯同时扫向三人，远处几名持枪的黑旗成员从货架间探出身影。腐味屏蔽剂被风卷散，尸群没有被完全引开，铁门外立刻响起杂乱的抓挠声。顾岚贴住转角，抬枪封住北侧出口；赵铁压低身体，死死盯着两辆车的轮距；陆沉则绕向普通尸群涌来的侧面，三人把追截线一点点收紧。真正的车，必须在下一次转向之前留下来。\n\n陆沉刚绕过尸群侧翼，耳机里突然切进苏晴的声音，压得很低，却没有半点迟疑：“把名册照片放大，第三列的前缀是LJ-赤潮-04。”\n\n赵铁抬眼看向迎面冲来的两辆车。它们的车厢、车灯和伪装泥点几乎一模一样，连后厢门上的划痕都像是被刻意复制出来的。陆沉耳机里传来林夏的确认：“照片已经转回消防站一层车库区，苏晴要求按风险公开条款直接通报。”\n\n短暂的电流杂音后，苏晴自己接上了频道。\n\n“我在爆发前接收过这批早期临床样本。”她说，“我只负责接收、登记和转交，编码前缀我认得，但我不知道完整的实验目的，也没有参与后续处置。这个事实可以核验，不能替我洗清，也不能直接给我定罪。”\n\n频道里静了两秒。陆沉没有追问，赵铁也没有回头。前车趁着尸群扑向铁门的空隙猛打方向，后车则突然横过来，枪口从车窗里探出。顾岚贴着转角开火，子弹打碎后车外壳上的假编号，逼得它向南偏移。\n\n“不是前车！”赵铁忽然吼道。\n\n他盯住两辆车的底盘，在交错的车灯里比出一个手势：“消防站原配滤芯序列的接口磨损在右侧，前车的磨损方向对不上。还有重量——前车起步太轻，后车压得悬挂下沉，真正的主滤芯在后面！”\n\n顾岚立刻将一枚震爆罐掷进转向口，爆响封住南侧的维修岔道。后车司机猛踩刹车，车轮擦着水泥地甩出一串火星。与此同时，陆沉冲进普通尸群的侧面，借尸体撞击车头的一瞬扣住一具歪斜的铁架，狠狠拖倒在路中央。真车退路被截断，驾驶室里的黑旗枪手推门探出，腐味屏蔽剂的气味混在风里，却没能骗过他的视线。\n\n枪声贴着陆沉耳侧掠过。他伏低身体翻到车尾，顾岚从侧面压住射界，赵铁则扑向后厢的锁扣。车内变异体撞得厢板咚咚作响，外面的尸群也被枪声重新吸引过来。三人没有贸然打开车厢，只用钢索先锁住后轴，把发动机彻底逼停。\n\n耳机里，苏晴的声音仍在发颤，却清晰地报出那串前缀：“LJ-赤潮-04之后，主滤芯序列会接续到M-17。别信车门上的批次号，看接口内圈。”\n\n赵铁撬开观察盖，灯光扫过内圈的磨损，抬头与陆沉对视。真正的车，已经被他们截在维修道中央。\n\n赵铁把观察盖彻底掀开，先用消防站原配序列核对接口，再摸过内圈那道被反复插拔留下的磨损痕。车厢里传出沉闷的金属碰撞声，他示意陆沉和顾岚退开，撬棍从侧缝探进去，压下车底的锁扣。\n\n“外壳批次能伪造，接口磨损伪造不了。”赵铁咬住手电，抬起电子秤复核运输重量，“这辆的载重与两组主滤芯和防震架的合计重量相符；前车起步太轻，里面才是配重和空壳。”\n\n维修道尽头，顾岚已经把两辆废弃货车横过来，封死了通往南侧转向口的路。陆沉没有再启用弱点视界，只抄起路边的钢管，一脚踢翻滚来的油桶。腐味屏蔽剂混着尸臭漫开，普通尸群被气味牵引着撞向车尾，却骗不过黑旗枪手。车顶的射孔立刻喷出火舌，一名枪手朝陆沉连续点射，另一个黑影则从车厢侧面扑下，四肢落地时带起一片不正常的骨响。\n\n顾岚压低身子，连开两枪逼退枪手；赵铁趁着车门偏转，将高压撬杆插进轮轴。陆沉借尸群撞击的间隙绕到侧后方，一钢管砸断变异体的前肢，再用绞索勒住它的颈骨。赵铁猛拧阀门，车底锁扣终于松开。三人合力拖出第一只铅封箱，第二只箱子的外壳却在碰撞中裂开，细碎的保温层从缝里簌簌掉落。\n\n“先封，不拆。”赵铁按住顾岚伸向卡扣的手，“编号只能确认到外壳。完好的这一组是M-17-04，裂开的也是M-17-05，但必须送回去待检，不能算完好备用。”\n\n耳机里沉默了两秒，林夏接过通话：“物流园现场不宣布恢复净水。名册、在场证言、控制器记录，全部按公开事实登记；至于谁在背后统筹，单列为推测，不许写成结论。”\n\n顾岚从驾驶座下摸出一本黑皮薄册，纸页边缘沾着暗红色的污迹。名册上密密麻麻列着多个地点，后面统一盖着“已交付”，另有几行被粗笔圈出，标注着“清扫优先级”，却没有组织名称、人员总表，甚至没有一张能拼出全貌的路线图。苏晴的声音再次插入频道，报出与早期临床样本相同的编码前缀。林夏当场复述她的证言：苏晴在爆发前只负责接收和登记样本，不知道完整的实验目的。那句话没有替她洗清嫌疑，也没有替任何人定罪，却让M-17序列和名册上的交付批次第一次彼此咬合。\n\n赵铁用钢带把两组滤芯分别固定，给裂壳箱贴上红色的待检封条。屋顶方向忽然传来低沉的蓄能声，终端倒计时跳成四小时四十五分。三人抬头时，远处一辆指挥车正调头驶向东街，车顶的黑旗在夜风里骤然绷直。\n\n那辆指挥车没有追向北院，反而猛地切进东街。梁魁显然已经放弃了主滤芯，车厢后门敞着，便携清扫控制器被人用绳带固定在车尾，几名黑旗枪手护在两侧。车头前方绑着三个人，手腕和脚踝都被反缚在保险杠支架上，身上各挂着一枚闪烁的信标。车轮每颠一下，三人的身体就被拖得撞向车架，微弱的挣扎在夜色里断断续续。\n\n“那是黑旗自己的人。”顾岚压低声音，望着其中一人胸口撕开的黑旗标记，“他们反对梁魁，所以被当成了新的活体信标。”\n\n陆沉已经抬起枪，却没有扣下扳机。车顶黑旗转向消防站——梁魁想用那三个人引开沿线尸群，也想逼站里的人开门。贸然追上去，只会让活体信标先死在他们眼前。\n\n“别追。”陆沉收枪，“先保住滤芯，再断主控。”\n\n赵铁应了一声，俯身检查刚夺下的两只箱体。他把钢带重新绕过车厢内的固定环，又在完好封装的箱角压上消防站原配铅封；另一只裂壳箱则加了两道横向束带，红色待检封条没有撕动半分。“两组都还在，编号分别是M-17-04和M-17-05。裂壳这一组不能接入净水线，运输震裂的位置还得回站里拆检。”\n\n顾岚展开纸质出库联，把物流园外围维修道到外围维修道北侧通道的路线逐段对照。维修道内侧的八名获救者仍由仓管员和两名志愿留下的非战斗人员看护，没有人被卷入追车；装卸通道也没有出现追车队伍。她确认完转移路线，才把视线投向屋顶：“人能按原路撤，车和滤芯却不能一起走。屋顶主控还在蓄能，下一轮启动前必须有人把它拆掉。”\n\n陆沉看向东街尽头。指挥车已经抢先驶向消防站，车前三枚信标在雾里一明一灭。杀死梁魁不是眼下的首要目标，真正逼近的，是屋顶那台仍在运行的物流园主控。赵铁留下固定两组滤芯，顾岚负责盯住撤离路线，陆沉则抬头确认通往屋顶的维修梯。三人没有追车，而是转身朝主控所在的楼顶准备推进。\n",
      "policy": "review_tail",
      "ranges": [
        {
          "start_char": 0,
          "end_char": 3216
        }
      ],
      "estimated_tokens": 804,
      "truncated": false,
      "source_sha256": "6de27e2d49d089daa662f7bae73608d6e80d8697fbe148ba09785d5f1a818789",
      "original_chars": 3216,
      "selected_items": [
        {
          "id": "document",
          "sha256": "6de27e2d49d089daa662f7bae73608d6e80d8697fbe148ba09785d5f1a818789",
          "original_chars": 3216,
          "start_char": 0,
          "end_char": 3216
        }
      ],
      "omitted_count": 0
    },
    "committed_verified": true
  },
  "semantic_state": {
    "schema_version": "1.0",
    "book_id": "4aefb5be-5459-4904-a07a-5fdadd5270fb",
    "chapter_index": 26,
    "parser_version": "shadow-1.0",
    "layout_profile_version": "legacy-zh-1",
    "source_digest": "ef0cd2339908f959df39efd4e71c6faca744d224a6d23ad2d9abcdb203f333da",
    "provenance_count": 97,
    "blocking_conflict_count": 0,
    "parse_warning_count": 1,
    "unsupported_excerpt_count": 11
  },
  "semantic_audit": {
    "configured_mode": "strict",
    "effective_mode": "strict",
    "authoritative": true,
    "profile_match": true,
    "downgraded": false,
    "ready_for_next_step": true,
    "blockers": [],
    "parser_version": "shadow-1.0",
    "semantic_schema_version": "1.0",
    "layout_profile_version": "legacy-zh-1",
    "source_digest": "ef0cd2339908f959df39efd4e71c6faca744d224a6d23ad2d9abcdb203f333da",
    "provenance_count": 97,
    "blocking_conflict_count": 0,
    "warning_count": 1,
    "parser_authoritative": true,
    "canonical_authoritative": false
  },
  "memory_v2": {
    "status": "ready",
    "revision": 16,
    "projection_hash": "1749c7b60bc5fa7eee11f2186f02b69a0dcbd180a71b3add523f7701e7302dc1"
  },
  "tracking_excerpts": {
    "上下文.md": {
      "relative_path": "追踪/上下文.md",
      "sha256": "2af0bcf647f0f4f89ba07273706bc24867da4eef3f28c68ac63ad4857652a6d6",
      "original_chars": 14587,
      "excerpt": "【侵蚀值：6/100】\n\n[…完整条目已省略…]\n\n<!-- NovelAgent:story_project_writeback run_id=chapter_20_20260804T045734824708Z chapter=020 target=context -->\n## 第020章 · 第二十章：火种落在消防站\n\n[…完整条目已省略…]\n\n<!-- NovelAgent:story_project_writeback run_id=chapter_21_20260811T074344579619Z chapter=021 target=context -->\n## 第021章 · 第21章 九小时清扫令\n\n[…完整条目已省略…]\n\n<!-- NovelAgent:story_project_writeback run_id=chapter_22_20260812T002724344776Z chapter=022 target=context -->\n## 第022章 · 第22章 不拿活人引尸\n\n[…完整条目已省略…]\n\n<!-- NovelAgent:semantic-state version=1 -->\n## NovelAgent Managed Projection\n\n- scope: context\n- run_id: chapter_25_20260901T040407553632Z\n- chapter: 25\n\n### Managed Fields\n- `story_state.last_chapter_ending`: \"三人没有追车，而是转身朝主控所在的楼顶准备推进。\"\n- `story_state.last_scene_characters`: [\"赵铁\",\"陆沉\",\"顾岚\"]\n- `story_state.last_scene_location`: \"物流园外围维修道\"\n- `story_state.open_threads`: []\n- `story_state.required_opening_bridge`: \"物流园外围维修道；赵铁、陆沉、顾岚\"\n\n```json\n{\n  \"base_revision\": \"9e881c4bf39ba6cd3276996a325fb08b56528508c21e9397985b68a7a7c56a09\",\n  \"base_source_digest\": \"f573bc95c404d089a036bc408be5638e7e7a4fe170996b143c775a02c1ecd2df\",\n  \"book_id\": \"4aefb5be-5459-4904-a07a-5fdadd5270fb\",\n  \"chapter\": 25,\n  \"owned_fields\": [\n    \"story_state.last_chapter_ending\",\n    \"story_state.last_scene_characters\",\n    \"story_state.last_scene_location\",\n    \"story_state.open_threads\",\n    \"story_state.required_opening_bridge\"\n  ],\n  \"parser_version\": \"shadow-1.0\",\n  \"payload_sha256\": \"e143897bbe15c9b67a1ce3ae7903df4a06f8f4df16f959a728edd1a405e909f2\",\n  \"run_id\": \"chapter_25_20260901T040407553632Z\",\n  \"schema_version\": \"1.0\",\n  \"scope\": \"context\",\n  \"tombstones\": [],\n  \"values\": {\n    \"story_state.last_chapter_ending\": \"三人没有追车，而是转身朝主控所在的楼顶准备推进。\",\n    \"story_state.last_scene_characters\": [\n      \"赵铁\",\n      \"陆沉\",\n      \"顾岚\"\n    ],\n    \"story_state.last_scene_location\": \"物流园外围维修道\",\n    \"story_state.open_threads\": [],\n    \"story_state.required_opening_bridge\": \"物流园外围维修道；赵铁、陆沉、顾岚\"\n  }\n}\n```\n<!-- /NovelAgent:semantic-state -->",
      "source_selection": {
        "schema_version": "1.0",
        "policy": "story_project_source_paragraph_relevance_v1",
        "source_sha256": "2af0bcf647f0f4f89ba07273706bc24867da4eef3f28c68ac63ad4857652a6d6",
        "original_chars": 14587,
        "selected_items": [
          {
            "id": "document",
            "sha256": "2af0bcf647f0f4f89ba07273706bc24867da4eef3f28c68ac63ad4857652a6d6",
            "original_chars": 14587,
            "start_char": 0,
            "end_char": 14587
          }
        ],
        "omitted_count": 0
      },
      "selection": {
        "schema_version": "1.0",
        "policy": "input_pack_story_source_paragraph_relevance_v1",
        "source_sha256": "2af0bcf647f0f4f89ba07273706bc24867da4eef3f28c68ac63ad4857652a6d6",
        "original_chars": 14587,
        "selected_items": [
          {
            "id": "paragraph:23",
            "sha256": "aa131533ccb3363cd0a0aac2710bdbef0609f99671a254769b0d744031032df9",
            "original_chars": 11,
            "start_char": 4526,
            "end_char": 4537
          },
          {
            "id": "paragraph:49",
            "sha256": "84bd272f143b302489872d8ab9afeb79a7ec0d34b26674a34e18753371d76a28",
            "original_chars": 135,
            "start_char": 10949,
            "end_char": 11084
          },
          {
            "id": "paragraph:51",
            "sha256": "00df09352b040d01b3644cea589b5def93f8029918079725678a0331e0ed8181",
            "original_chars": 134,
            "start_char": 11760,
            "end_char": 11894
          },
          {
            "id": "paragraph:53",
            "sha256": "f7c8c11f03a438d5e6fbe2cf2acbb2f79a5ca1123474ff9d248b9db243907352",
            "original_chars": 134,
            "start_char": 12441,
            "end_char": 12575
          },
          {
            "id": "paragraph:55",
            "sha256": "6992807e57de9b411863621127279b07c2c6077defe5a75bc87c75ce63c6cf25",
            "original_chars": 77,
            "start_char": 13071,
            "end_char": 13148
          },
          {
            "id": "paragraph:56",
            "sha256": "ba567af76df9341a2d868d8da770ee6327ef6c65b8f26141bc5e119e2b465c6d",
            "original_chars": 74,
            "start_char": 13150,
            "end_char": 13224
          },
          {
            "id": "paragraph:57",
            "sha256": "b96b65e35d05ed938c31a106cd40fac15ed4134ea7c6c113c7b3d58263e131d4",
            "original_chars": 280,
            "start_char": 13226,
            "end_char": 13506
          },
          {
            "id": "paragraph:58",
            "sha256": "8fb8054254517b2b167234b043b4f5d935637be2ed4876516902beed3b666ba8",
            "original_chars": 1078,
            "start_char": 13508,
            "end_char": 14586
          }
        ],
        "omitted_count": 51
      },
      "truncated": true
    },
    "伏笔.md": {
      "relative_path": "追踪/伏笔.md",
      "sha256": "45bd1e984ebe8059a19c19fff546102a27b04adc8d4f4812e894cb4202ddc503",
      "original_chars": 12478,
      "excerpt": "# 伏笔\n\n[…完整条目已省略…]\n\n<!-- NovelAgent:story_project_writeback run_id=chapter_20_20260804T045734824708Z chapter=020 target=foreshadowing -->\n## 第020章 · 第二十章：火种落在消防站\n\n[…完整条目已省略…]\n\n<!-- NovelAgent:story_project_writeback run_id=chapter_21_20260811T074344579619Z chapter=021 target=foreshadowing -->\n## 第021章 · 第21章 九小时清扫令\n\n- run_id: chapter_21_20260811T074344579619Z\n- source: NovelAgent committed run\n- blueprint_coverage: covered_beat_indexes=[1, 2, 3, 4, 5, 6, 7, 8], ending_pressure_covered=True, ending_pressure_required=True, required_beat_count=8\n- summary: 消防站一层车库区里，单独两小时观察区的记录刚落下最后一笔，车库里的双重警报又被切成了低沉的蜂鸣。 林夏站在值勤板前，苏晴守着高压泵运输车外的隔离黄线，赵铁和韩野已经蹲到泵车旁，陆沉站在泵车与净化机组之间，视线扫过那道尚未解除的黄线。\n- conflicts: 代价\n<!-- /NovelAgent:story_project_writeback -->\n\n<!-- NovelAgent:story_project_writeback run_id=chapter_22_20260812T002724344776Z chapter=022 target=foreshadowing -->\n## 第022章 · 第22章 不拿活人引尸\n\n- run_id: chapter_22_20260812T002724344776Z\n- source: NovelAgent committed run\n- blueprint_coverage: covered_beat_indexes=[1, 2, 3, 4, 5, 6, 7, 8], ending_pressure_covered=True, ending_pressure_required=True, required_beat_count=8\n- summary: 八小时十二分。 单独两小时观察区仍在内门后，原有四名获救者继续留在隔离线内。\n- conflicts: 未记录\n<!-- /NovelAgent:story_project_writeback -->\n\n<!-- NovelAgent:semantic-state version=1 -->\n## NovelAgent Managed Projection\n\n- scope: foreshadowing\n- run_id: chapter_25_20260901T040407553632Z\n- chapter: 25\n\n### Managed Fields\n- (none)\n\n```json\n{\n  \"base_revision\": \"1b929f5f5d4eea322c669274895d74b52bc4e58c31068b01ca30f4cd66058b4c\",\n  \"base_source_digest\": \"3b74e102dfccd948a4cb8e7b6a4d6ec07543cf97694fc83a3d7c34770eb2979e\",\n  \"book_id\": \"4aefb5be-5459-4904-a07a-5fdadd5270fb\",\n  \"chapter\": 25,\n  \"owned_fields\": [],\n  \"parser_version\": \"shadow-1.0\",\n  \"payload_sha256\": \"a31d1274772b7f2ef86537f09e1ee5dd65c93742322942f0a2cb816428f7473f\",\n  \"run_id\": \"chapter_25_20260901T040407553632Z\",\n  \"schema_version\": \"1.0\",\n  \"scope\": \"foreshadowing\",\n  \"tombstones\": [],\n  \"values\": {}\n}\n```\n<!-- /NovelAgent:semantic-state -->",
      "source_selection": {
        "schema_version": "1.0",
        "policy": "story_project_source_paragraph_relevance_v1",
        "source_sha256": "45bd1e984ebe8059a19c19fff546102a27b04adc8d4f4812e894cb4202ddc503",
        "original_chars": 12478,
        "selected_items": [
          {
            "id": "document",
            "sha256": "45bd1e984ebe8059a19c19fff546102a27b04adc8d4f4812e894cb4202ddc503",
            "original_chars": 12478,
            "start_char": 0,
            "end_char": 12478
          }
        ],
        "omitted_count": 0
      },
      "selection": {
        "schema_version": "1.0",
        "policy": "input_pack_story_source_paragraph_relevance_v1",
        "source_sha256": "45bd1e984ebe8059a19c19fff546102a27b04adc8d4f4812e894cb4202ddc503",
        "original_chars": 12478,
        "selected_items": [
          {
            "id": "paragraph:0",
            "sha256": "d32a865baabb57d8554ff13368b7f8e52c037ccbde2744fbd805b90719cb16f3",
            "original_chars": 4,
            "start_char": 0,
            "end_char": 4
          },
          {
            "id": "paragraph:41",
            "sha256": "d46f969f676170fd4cbdd788d2c2cde423e68d015c0c4c1452381626ef2d2d42",
            "original_chars": 141,
            "start_char": 10118,
            "end_char": 10259
          },
          {
            "id": "paragraph:43",
            "sha256": "25f9ebecd45b92d8415e6ca5b5bd352e272f149f5c6a65460b0ad9516eaeaec1",
            "original_chars": 140,
            "start_char": 10655,
            "end_char": 10795
          },
          {
            "id": "paragraph:44",
            "sha256": "6f8f6fe33b260c24d6f79196f99a37ebad8c3d7028c3ef37102ec29a4cab4ba2",
            "original_chars": 419,
            "start_char": 10797,
            "end_char": 11216
          },
          {
            "id": "paragraph:45",
            "sha256": "a46b1f6c1d931f41db10aa51b4dac4bc1afba69f5e310bdbd3b85bb92401ec98",
            "original_chars": 140,
            "start_char": 11218,
            "end_char": 11358
          },
          {
            "id": "paragraph:46",
            "sha256": "897ea7c113428031fd05aefe29bff554645d86f20e2cda659bd39ad9557e9abf",
            "original_chars": 342,
            "start_char": 11360,
            "end_char": 11702
          },
          {
            "id": "paragraph:47",
            "sha256": "6992807e57de9b411863621127279b07c2c6077defe5a75bc87c75ce63c6cf25",
            "original_chars": 77,
            "start_char": 11704,
            "end_char": 11781
          },
          {
            "id": "paragraph:48",
            "sha256": "f25e610171687e1e3334d826cc9cee9df214b48d16747515a3bf0879fe729bc6",
            "original_chars": 80,
            "start_char": 11783,
            "end_char": 11863
          },
          {
            "id": "paragraph:49",
            "sha256": "04023e5597768f96834cd99bdcf37d9afdaa9cc24d04eb8fe651b785a021bcb1",
            "original_chars": 27,
            "start_char": 11865,
            "end_char": 11892
          },
          {
            "id": "paragraph:50",
            "sha256": "8a7cdb665cb316a3a863e3e1dbef03341c780c06e4fd57494762e026c09b393f",
            "original_chars": 583,
            "start_char": 11894,
            "end_char": 12477
          }
        ],
        "omitted_count": 41
      },
      "truncated": true
    },
    "时间线.md": {
      "relative_path": "追踪/时间线.md",
      "sha256": "ae126b4ef21ada884763fc42ac9b71ae38441dbfb1fa829cacb82e28baba1fd6",
      "original_chars": 17462,
      "excerpt": "【侵蚀值：6/100】\n\n[…完整条目已省略…]\n\n```json\n{\n  \"base_revision\": \"b3143b7c591800372d9d5d7b00c8a92e2723b1bd1a5d7d13b3cad0c7a624491c\",\n  \"base_source_digest\": \"ea5986c6b89c88792b098c23e184e14aa7c964d5607fda647c6b2ea77961ce75\",\n  \"book_id\": \"4aefb5be-5459-4904-a07a-5fdadd5270fb\",\n  \"chapter\": 25,\n  \"owned_fields\": [\n    \"timeline.chapter-000023\",\n    \"timeline.chapter-000024\",\n    \"timeline.chapter-000025\"\n  ],\n  \"parser_version\": \"shadow-1.0\",\n  \"payload_sha256\": \"6a189b3ea24f1f00fe3c7938dfb62833823225aae30e2f32a808f41e12298b30\",\n  \"run_id\": \"chapter_25_20260901T040407553632Z\",\n  \"schema_version\": \"1.0\",\n  \"scope\": \"timeline\",\n  \"tombstones\": [],\n  \"values\": {\n    \"timeline.chapter-000023\": {\n      \"chapter_index\": 23,\n      \"events\": [\n        {\n          \"text\": \"顾岚用镜片反光扫过墙角，发现一根被重新接过的信号线，线头绕向后廊，却没有人守在明处。\"\n        }\n      ],\n      \"id\": \"chapter-000023\",\n      \"summary\": \"消防救援车从安全交汇点拐入城西物流园外围的维修道时，四名新临时受保护者已经交给接应车，沿另一条路线返回消防站双门验伤区。 车轮压过碎石，车内只剩陆沉、赵铁和顾岚三人。\",\n      \"world_changes\": []\n    },\n    \"timeline.chapter-000024\": {\n      \"chapter_index\": 24,\n      \"events\": [\n        {\n          \"text\": \"赵铁把八人的人数、伤势和发现位置记进纸条，塞入防水袋。\"\n        }\n      ],\n      \"id\": \"chapter-000024\",\n      \"summary\": \"十分钟倒计时还剩八分四十七秒。 负二层冷库外，北院方向传来叉车倒车的蜂鸣，隔着厚墙也能听见主滤芯箱被推上车板的沉闷撞击。\",\n      \"world_changes\": [\n        {\n          \"text\": \"Infection pressure is active in the chapter.\",\n          \"type\": \"infection_pressure\"\n        }\n      ]\n    },\n    \"timeline.chapter-000025\": {\n      \"chapter_index\": 25,\n      \"events\": [\n        {\n          \"text\": \"真正的车，必须在下一次转向之前留下来。\"\n        },\n        {\n          \"text\": \"完好的这一组是M-17-04，裂开的也是M-17-05，但必须送回去待检，不能算完好备用。\"\n        },\n        {\n          \"text\": \"屋顶主控还在蓄能，下一轮启动前必须有人把它拆掉。\"\n        }\n      ],\n      \"id\": \"chapter-000025\",\n      \"summary\": \"在物流园外围维修道，北院方向的发动机声刚从雾里传来，陆沉便回头看了一眼那辆被弃在路边的消防救援车。 八名刚救出的获救者已经转移到维修道内侧，没有人被允许参加追车；仓管员带着两名自愿留下的非战斗人员守在他们身边，按苏晴交代逐人看护，伤者的转移和验伤都不因外面的追截而中断。\",\n      \"world_changes\": []\n    }\n  }\n}\n```\n<!-- /NovelAgent:semantic-state -->",
      "source_selection": {
        "schema_version": "1.0",
        "policy": "story_project_source_paragraph_relevance_v1",
        "source_sha256": "ae126b4ef21ada884763fc42ac9b71ae38441dbfb1fa829cacb82e28baba1fd6",
        "original_chars": 17462,
        "selected_items": [
          {
            "id": "document",
            "sha256": "ae126b4ef21ada884763fc42ac9b71ae38441dbfb1fa829cacb82e28baba1fd6",
            "original_chars": 17462,
            "start_char": 0,
            "end_char": 17462
          }
        ],
        "omitted_count": 0
      },
      "selection": {
        "schema_version": "1.0",
        "policy": "input_pack_story_source_paragraph_relevance_v1",
        "source_sha256": "ae126b4ef21ada884763fc42ac9b71ae38441dbfb1fa829cacb82e28baba1fd6",
        "original_chars": 17462,
        "selected_items": [
          {
            "id": "paragraph:21",
            "sha256": "aa131533ccb3363cd0a0aac2710bdbef0609f99671a254769b0d744031032df9",
            "original_chars": 11,
            "start_char": 4911,
            "end_char": 4922
          },
          {
            "id": "paragraph:56",
            "sha256": "148006f27fe7ca81d27399ed8e6bca68d13a2cb5773f2e581dadf2013010047b",
            "original_chars": 1962,
            "start_char": 15499,
            "end_char": 17461
          }
        ],
        "omitted_count": 55
      },
      "truncated": true
    },
    "角色状态.md": {
      "relative_path": "追踪/角色状态.md",
      "sha256": "137edd9cd0adc9a16a8470684828a531f2f8d8dbc081798c252a9eec199c514c",
      "original_chars": 13745,
      "excerpt": "## 陆沉\n\n[…完整条目已省略…]\n\n## 苏晴\n\n尚未与陆沉汇合，位于一层医药专柜附近。\n\n## 赵铁\n\n尚未与陆沉汇合，位于地下停车场卸货区。\n\n[…完整条目已省略…]\n\n<!-- NovelAgent:story_project_writeback run_id=chapter_22_20260812T002724344776Z chapter=022 target=character_state -->\n## 第022章 · 第22章 不拿活人引尸\n\n[…完整条目已省略…]\n\n<!-- NovelAgent:semantic-state version=1 -->\n## NovelAgent Managed Projection\n\n- scope: character_state\n- run_id: chapter_25_20260901T040407553632Z\n- chapter: 25\n\n### Managed Fields\n- `spatial_state.blocked_paths`: []\n- `spatial_state.character_positions`: {}\n- `spatial_state.connections`: []\n- `spatial_state.last_transition`: {\"source\":\"chapter_analysis\",\"to\":\"物流园外围维修道\"}\n- `spatial_state.spaces`: {\"消防站一层车库区\":{\"source\":\"chapter_analysis\"},\"消防站独立隔离柜\":{\"source\":\"chapter_analysis\"},\"物流园外围维修道\":{\"source\":\"chapter_analysis\"}}\n\n```json\n{\n  \"base_revision\": \"260c889362b374d6061cf512d3b3f5c51fc58f0401a76bab0e67492607c9830c\",\n  \"base_source_digest\": \"db496362da2da6686976d7828603272b60a9193e6c55776e677cc43cf4fc2edc\",\n  \"book_id\": \"4aefb5be-5459-4904-a07a-5fdadd5270fb\",\n  \"chapter\": 25,\n  \"owned_fields\": [\n    \"spatial_state.blocked_paths\",\n    \"spatial_state.character_positions\",\n    \"spatial_state.connections\",\n    \"spatial_state.last_transition\",\n    \"spatial_state.spaces\"\n  ],\n  \"parser_version\": \"shadow-1.0\",\n  \"payload_sha256\": \"57b252594ad2576f128f01cf52bac3a125b7ac5f53b2aae989e5580ca0ada802\",\n  \"run_id\": \"chapter_25_20260901T040407553632Z\",\n  \"schema_version\": \"1.0\",\n  \"scope\": \"character_state\",\n  \"tombstones\": [],\n  \"values\": {\n    \"spatial_state.blocked_paths\": [],\n    \"spatial_state.character_positions\": {},\n    \"spatial_state.connections\": [],\n    \"spatial_state.last_transition\": {\n      \"source\": \"chapter_analysis\",\n      \"to\": \"物流园外围维修道\"\n    },\n    \"spatial_state.spaces\": {\n      \"消防站一层车库区\": {\n        \"source\": \"chapter_analysis\"\n      },\n      \"消防站独立隔离柜\": {\n        \"source\": \"chapter_analysis\"\n      },\n      \"物流园外围维修道\": {\n        \"source\": \"chapter_analysis\"\n      }\n    }\n  }\n}\n```\n<!-- /NovelAgent:semantic-state -->",
      "source_selection": {
        "schema_version": "1.0",
        "policy": "story_project_source_paragraph_relevance_v1",
        "source_sha256": "137edd9cd0adc9a16a8470684828a531f2f8d8dbc081798c252a9eec199c514c",
        "original_chars": 13745,
        "selected_items": [
          {
            "id": "document",
            "sha256": "137edd9cd0adc9a16a8470684828a531f2f8d8dbc081798c252a9eec199c514c",
            "original_chars": 13745,
            "start_char": 0,
            "end_char": 13745
          }
        ],
        "omitted_count": 0
      },
      "selection": {
        "schema_version": "1.0",
        "policy": "input_pack_story_source_paragraph_relevance_v1",
        "source_sha256": "137edd9cd0adc9a16a8470684828a531f2f8d8dbc081798c252a9eec199c514c",
        "original_chars": 13745,
        "selected_items": [
          {
            "id": "paragraph:1",
            "sha256": "5626552d9ae07555a593ff458033207b0f565d2c4ed8a64e4668c96dfea72030",
            "original_chars": 5,
            "start_char": 8,
            "end_char": 13
          },
          {
            "id": "paragraph:3",
            "sha256": "1c4e5b6b73909a2aca1e6f215e9e9e2efd8458503f6fbe766caaae51528a96a2",
            "original_chars": 5,
            "start_char": 154,
            "end_char": 159
          },
          {
            "id": "paragraph:4",
            "sha256": "134d42d0a12cac75abf83ab21cdc7e1558974a922f3e6e9fdcf2357d1900b30b",
            "original_chars": 19,
            "start_char": 161,
            "end_char": 180
          },
          {
            "id": "paragraph:5",
            "sha256": "f7480f4439dee0f255002ca9ca4f1c2216016dc5278ff2aeb5fb67b9688d7439",
            "original_chars": 5,
            "start_char": 182,
            "end_char": 187
          },
          {
            "id": "paragraph:6",
            "sha256": "d67e12396ba10112175aea1c10798b608ef6b20201929f463c910f11358f7f9c",
            "original_chars": 19,
            "start_char": 189,
            "end_char": 208
          },
          {
            "id": "paragraph:51",
            "sha256": "eb9933a51d22b538956c3072bb3fda427319d50a06cfc71307036ba4212ad36b",
            "original_chars": 142,
            "start_char": 11498,
            "end_char": 11640
          },
          {
            "id": "paragraph:53",
            "sha256": "6992807e57de9b411863621127279b07c2c6077defe5a75bc87c75ce63c6cf25",
            "original_chars": 77,
            "start_char": 11994,
            "end_char": 12071
          },
          {
            "id": "paragraph:54",
            "sha256": "cbdb49396504a6c47c9a0eceb2b4f3f325b8851609fe788f56bdf57cd5920a54",
            "original_chars": 82,
            "start_char": 12073,
            "end_char": 12155
          },
          {
            "id": "paragraph:55",
            "sha256": "0abcef841f28b8c30218b0bd1857e40e321524e7a3db0a7d99441612c7352066",
            "original_chars": 362,
            "start_char": 12157,
            "end_char": 12519
          },
          {
            "id": "paragraph:56",
            "sha256": "1b022fe2702dd9e8e7cb98b47ceb6d362bc7cfc1d4819b9ee6d52ebb9e59a5cd",
            "original_chars": 1223,
            "start_char": 12521,
            "end_char": 13744
          }
        ],
        "omitted_count": 47
      },
      "truncated": true
    }
  },
  "setting_excerpts": {
    "作品设定.md": {
      "relative_path": "设定/作品设定.md",
      "sha256": "f9465e44318e82f65b55cf0c9074f5cf5a6efe5a6bb89ac02eb691ba72cc30a6",
      "original_chars": 913,
      "excerpt": "# 《尸潮之下：我能提取进化词条》\n\n## 基本定位\n\n- 类型：丧尸末日、生存、人性抉择、能力升级、基地经营、热血爽文\n- 目标气质：危机压迫感与高密度反击并行，升级有明确反馈，胜利始终伴随代价\n- 核心卖点：主角击杀变异体后能看见并提取“进化词条”，还能将词条赋予装备、设施或可信任的同伴\n- 长线悬念：所谓词条并非系统奖励，而是感染源筛选“新物种”的实验记录\n\n## 故事梗概\n\n赤潮病毒在七十二分钟内击穿临江市。仓储安全员陆沉被困在地下商超，亲眼看见秩序比城市更早崩塌。濒死时，他发现自己能从变异体身上提取进化词条：力量、感知、再生，甚至罕见的规则能力。\n\n别人争抢最后一包食物时，陆沉已经开始清理尸群、改造据点、建立队伍。但每吸收一枚词条，他的“侵蚀值”也会增长；越接近怪物的力量，越可能失去作为人的底线。\n\n他要在尸潮、掠夺者、进化教派与失控官方实验体之间杀出一条生路，也必须回答一个问题：末日里真正需要被保住的，究竟是人的生命，还是人之所以为人的东西？\n\n## 核心主题\n\n生存不是人性的反面，而是对人性的放大。力量让主角拥有选择权，而他如何使用选择权，决定他最终成为守住火种的人，还是更强大的怪物。\n\n## 爽点循环\n\n1. 资源或同伴遭遇明确危机。\n2. 主角用信息差与能力组合完成越级猎杀。\n3. 获得可见、可组合的词条奖励。\n4. 奖励立刻转化为战力、设施或团队优势。\n5. 新能力暴露更大世界与更强敌人，并带来侵蚀或信任代价。\n\n## 三阶段成长\n\n- 前期“活下来”：个人战斗、物资搜集、救人与取舍，建立地下商超安全屋。\n- 中期“立规矩”：扩建地铁枢纽基地，争夺水源、电力、药品与人口，和不同幸存者势力博弈。\n- 后期“夺回世界”：解析赤潮源头，建设人类进化序列，对抗母巢与试图以末日重塑阶级的幕后组织。\n\n## 首卷：断电之城（1—30章）\n\n陆沉从地下商超突围，组建最初的四人小队，占领消防站并夺回净水设备。卷末迎战第一次万级尸潮，公开处决把幸存者当诱饵的黑旗会首领，建立“火种营地”的三条铁律。\n\n首卷核心回报：从独自求生到拥有一座可升级基地；从被迫杀第一只丧尸到正面斩杀首只领主级变异体。\n",
      "source_selection": {
        "schema_version": "1.0",
        "policy": "story_project_source_paragraph_relevance_v1",
        "source_sha256": "f9465e44318e82f65b55cf0c9074f5cf5a6efe5a6bb89ac02eb691ba72cc30a6",
        "original_chars": 913,
        "selected_items": [
          {
            "id": "document",
            "sha256": "f9465e44318e82f65b55cf0c9074f5cf5a6efe5a6bb89ac02eb691ba72cc30a6",
            "original_chars": 913,
            "start_char": 0,
            "end_char": 913
          }
        ],
        "omitted_count": 0
      },
      "selection": {
        "schema_version": "1.0",
        "policy": "input_pack_story_source_paragraph_relevance_v1",
        "source_sha256": "f9465e44318e82f65b55cf0c9074f5cf5a6efe5a6bb89ac02eb691ba72cc30a6",
        "original_chars": 913,
        "selected_items": [
          {
            "id": "document",
            "sha256": "f9465e44318e82f65b55cf0c9074f5cf5a6efe5a6bb89ac02eb691ba72cc30a6",
            "original_chars": 913,
            "start_char": 0,
            "end_char": 913
          }
        ],
        "omitted_count": 0
      },
      "truncated": false
    },
    "地点/临江市地下商超.md": {
      "relative_path": "设定/地点/临江市地下商超.md",
      "sha256": "8d9f191f7225dc866a9755e1f10da074afe15883ffbc515bbe08817251f65fa2",
      "original_chars": 984,
      "excerpt": "# 临江市地下商超\n\n## 明确事实\n\n- 地下一层为营业区，地下二层为仓储区，西侧连通停车场，北侧有废弃防空通道。\n- 冷库拥有独立柴油发电机，但燃料只够连续运行十八小时。\n- 仓储区卷帘门可以隔绝普通丧尸，无法长期承受力量型变异体冲击。\n- 医药专柜与监控室分别位于营业区东南角和员工通道入口。\n\n## 员工通道门位\n\n- 监控室内门：连接监控室与员工通道内段，可正常开关。\n- 东侧外门：员工通道通往医药专柜方向的接应门，可由监控室控制。\n- 营业区侧门：感染者曾撞开的侧门，已被机械扣与倒塌货架封死，禁止再次通行。\n- 三扇门不是同一扇门；人物移动时必须明确门名。\n\n## 负二层与停车场\n\n- 负二层仓储外廊：由员工通道内段经西南侧消防楼梯抵达，连接冷库外廊与地下停车场。\n- 地下停车场卸货区：位于停车场西侧，设有卸货卷帘门、柴油发电机本地控制柜、货车泊位和手动叉车。\n- 地面物流坡道：从地下停车场通往地面，坡道防火门与冷库外廊北侧相连。\n- 物流坡道东侧检修门：坡道中段通往负二层卸货区的机械人员门；门内有一段缓冲走廊和可手动落下的防火卷帘，可作为逐人检查的临时双门隔离区。\n- 卸货区维修坑：位于货车泊位北侧，平时覆盖钢板；移开钢板后可困住大型感染体，但承重边缘可能坍塌。\n- 检修门外防撞柱：紧贴东侧检修门外墙的两根钢柱，与北侧卸货区维修坑相隔一段车道；适合用钢索将贴门目标锁死在门侧，不能与维修坑混写成同一位置。\n\n## 北侧废弃防空维护通道\n\n- 入口位于负二层仓储外廊北墙，入口防火门已被幸存者从通道内侧锁死。\n- 通道向北延伸，连接一处废弃运输中转间、旧人防医院支线与北站外围维护网，不直接进入北站站厅。\n- 通道内部有独立低压供电、红色引导灯和R-17运输标记，说明赤潮前曾用于隐秘实验样本转运。\n- 人防医院支线防爆门：位于R-17运输中转间东南侧，门后依次为侧向缓冲廊、废弃人防医院地下病区；与运输线前后钢滑门分属不同方向。\n- 人防医院地下病区：拥有护士站、可封闭病房、隔离室、药房、设备控制间与通风检修梯，可作为临时安全点；R-17回收程序能远程牵引外侧门锁，但机械断电后可阻止自动开门。\n\n## 战术价值\n\n这里物资丰富、通道复杂，适合作为前期求生场与教学关，但入口过多，不适合长期基地。第一章的断电会迫使幸存者暴露真实立场。\n",
      "source_selection": {
        "schema_version": "1.0",
        "policy": "story_project_source_paragraph_relevance_v1",
        "source_sha256": "8d9f191f7225dc866a9755e1f10da074afe15883ffbc515bbe08817251f65fa2",
        "original_chars": 984,
        "selected_items": [
          {
            "id": "document",
            "sha256": "8d9f191f7225dc866a9755e1f10da074afe15883ffbc515bbe08817251f65fa2",
            "original_chars": 984,
            "start_char": 0,
            "end_char": 984
          }
        ],
        "omitted_count": 0
      },
      "selection": {
        "schema_version": "1.0",
        "policy": "input_pack_story_source_paragraph_relevance_v1",
        "source_sha256": "8d9f191f7225dc866a9755e1f10da074afe15883ffbc515bbe08817251f65fa2",
        "original_chars": 984,
        "selected_items": [
          {
            "id": "document",
            "sha256": "8d9f191f7225dc866a9755e1f10da074afe15883ffbc515bbe08817251f65fa2",
            "original_chars": 984,
            "start_char": 0,
            "end_char": 984
          }
        ],
        "omitted_count": 0
      },
      "truncated": false
    },
    "能力体系.md": {
      "relative_path": "设定/能力体系.md",
      "sha256": "9d8082b1b1268f0de85f6d95c668c3b4dbea3f46aa325c56a00ff1ddc2272a2f",
      "original_chars": 650,
      "excerpt": "# 进化词条体系\n\n## 基础规则\n\n- 陆沉能看见感染体、特殊物品与极少数幸存者携带的词条。\n- 完成有效击杀或满足特殊条件后，才有概率提取词条。\n- 词条分为灰、白、蓝、紫、金、赤六阶，可升级、融合或转移。\n- 人体同时承载的词条有限；超载会增加侵蚀值，引发幻听、嗜血和人格剥离。\n- 将词条赋予装备或基地设施，不增加人体侵蚀，但需要更多材料与能量。\n- 提取必须由陆沉直接接触有效击杀后的目标或与目标相连的武器；向装备、门或设施转移时，陆沉必须同时维持“尸体—武器/钢索—自身—承载物”的连续接触链，且目标在三米内。接触链中断后本次词条不能远距离飞向设施。\n\n## 初期词条\n\n| 词条 | 品阶 | 效果 | 限制 |\n|---|---|---|---|\n| 应激爆发 | 白 | 危急时短暂提高力量与反应 | 结束后肌肉拉伤 |\n| 腐味屏蔽 | 蓝 | 短时间降低普通丧尸感知 | 流血时失效 |\n| 弱点视界 | 蓝 | 看见目标动作破绽与感染核心 | 精神消耗明显 |\n| 坚固 | 白 | 可赋予门、盾或护具 | 需要同类材料修复 |\n\n## 主角初始限制\n\n- 初始词条槽：2\n- 初始侵蚀值：0/100\n- 第一次提取必得“应激爆发”，但主角直到实战后才理解代价。\n- 侵蚀值超过30时出现感染者低语，超过60时产生攻击冲动，达到100将不可逆异化。\n\n## 升级原则\n\n每次升级必须同时产生三种变化：可见战力提升、战术选择增加、代价或责任扩大。禁止只用数值碾压消解所有冲突。\n",
      "source_selection": {
        "schema_version": "1.0",
        "policy": "story_project_source_paragraph_relevance_v1",
        "source_sha256": "9d8082b1b1268f0de85f6d95c668c3b4dbea3f46aa325c56a00ff1ddc2272a2f",
        "original_chars": 650,
        "selected_items": [
          {
            "id": "document",
            "sha256": "9d8082b1b1268f0de85f6d95c668c3b4dbea3f46aa325c56a00ff1ddc2272a2f",
            "original_chars": 650,
            "start_char": 0,
            "end_char": 650
          }
        ],
        "omitted_count": 0
      },
      "selection": {
        "schema_version": "1.0",
        "policy": "input_pack_story_source_paragraph_relevance_v1",
        "source_sha256": "9d8082b1b1268f0de85f6d95c668c3b4dbea3f46aa325c56a00ff1ddc2272a2f",
        "original_chars": 650,
        "selected_items": [
          {
            "id": "document",
            "sha256": "9d8082b1b1268f0de85f6d95c668c3b4dbea3f46aa325c56a00ff1ddc2272a2f",
            "original_chars": 650,
            "start_char": 0,
            "end_char": 650
          }
        ],
        "omitted_count": 0
      },
      "truncated": false
    },
    "角色/苏晴.md": {
      "relative_path": "设定/角色/苏晴.md",
      "sha256": "68e95eb0e302aae551d9bda7712f2b9c72d8987aeeb91ebdce001a86a62aed1a",
      "original_chars": 218,
      "excerpt": "# 苏晴\n\n| 字段 | 内容 |\n|---|---|\n| 年龄 | 30岁 |\n| 身份 | 急诊科医生 |\n| 性格 | 理性、克制、敢于质疑主角 |\n| 专长 | 创伤救治、感染观察、有限资源分诊 |\n| 初始秘密 | 曾参与赤潮病毒早期临床样本的接收工作 |\n| 与陆沉关系 | 价值观盟友，也是阻止陆沉越过人性底线的人 |\n\n苏晴坚持“救人必须有规则”，她不是无条件圣母。她能做残酷分诊，却绝不允许强者把残酷包装成理所当然。\n",
      "source_selection": {
        "schema_version": "1.0",
        "policy": "story_project_source_paragraph_relevance_v1",
        "source_sha256": "68e95eb0e302aae551d9bda7712f2b9c72d8987aeeb91ebdce001a86a62aed1a",
        "original_chars": 218,
        "selected_items": [
          {
            "id": "document",
            "sha256": "68e95eb0e302aae551d9bda7712f2b9c72d8987aeeb91ebdce001a86a62aed1a",
            "original_chars": 218,
            "start_char": 0,
            "end_char": 218
          }
        ],
        "omitted_count": 0
      },
      "selection": {
        "schema_version": "1.0",
        "policy": "input_pack_story_source_paragraph_relevance_v1",
        "source_sha256": "68e95eb0e302aae551d9bda7712f2b9c72d8987aeeb91ebdce001a86a62aed1a",
        "original_chars": 218,
        "selected_items": [
          {
            "id": "document",
            "sha256": "68e95eb0e302aae551d9bda7712f2b9c72d8987aeeb91ebdce001a86a62aed1a",
            "original_chars": 218,
            "start_char": 0,
            "end_char": 218
          }
        ],
        "omitted_count": 0
      },
      "truncated": false
    },
    "角色/赵铁.md": {
      "relative_path": "设定/角色/赵铁.md",
      "sha256": "bec0edb2cb6d7d05c243f2b1b37416b12a28a27ef5dee3173c106091be90c3ba",
      "original_chars": 212,
      "excerpt": "# 赵铁\n\n| 字段 | 内容 |\n|---|---|\n| 年龄 | 35岁 |\n| 身份 | 冷链货车司机 |\n| 性格 | 嘴碎、现实、讲义气，擅长缓解紧张气氛 |\n| 专长 | 驾驶、维修、路线判断、物资估价 |\n| 初始立场 | 只想带物资离开，不愿为陌生人冒险 |\n| 成长方向 | 从精致利己者成长为基地后勤负责人 |\n\n赵铁代表大多数普通人的求生逻辑。他会动摇、会害怕，也会在真正需要时把最后一脚油门踩到底。\n",
      "source_selection": {
        "schema_version": "1.0",
        "policy": "story_project_source_paragraph_relevance_v1",
        "source_sha256": "bec0edb2cb6d7d05c243f2b1b37416b12a28a27ef5dee3173c106091be90c3ba",
        "original_chars": 212,
        "selected_items": [
          {
            "id": "document",
            "sha256": "bec0edb2cb6d7d05c243f2b1b37416b12a28a27ef5dee3173c106091be90c3ba",
            "original_chars": 212,
            "start_char": 0,
            "end_char": 212
          }
        ],
        "omitted_count": 0
      },
      "selection": {
        "schema_version": "1.0",
        "policy": "input_pack_story_source_paragraph_relevance_v1",
        "source_sha256": "bec0edb2cb6d7d05c243f2b1b37416b12a28a27ef5dee3173c106091be90c3ba",
        "original_chars": 212,
        "selected_items": [
          {
            "id": "document",
            "sha256": "bec0edb2cb6d7d05c243f2b1b37416b12a28a27ef5dee3173c106091be90c3ba",
            "original_chars": 212,
            "start_char": 0,
            "end_char": 212
          }
        ],
        "omitted_count": 0
      },
      "truncated": false
    },
    "角色/陆沉.md": {
      "relative_path": "设定/角色/陆沉.md",
      "sha256": "a95f395f7022a11a4b555d924de2cee2190f90913853c2ee8bdc2bf046e3387a",
      "original_chars": 322,
      "excerpt": "# 陆沉\n\n| 字段 | 内容 |\n|---|---|\n| 年龄 | 27岁 |\n| 身份 | 大型仓储商超夜班安全员，退役消防救援队员 |\n| 性格 | 冷静、执行力强、不轻易许诺；一旦承诺便会死扛到底 |\n| 缺点 | 习惯独自承担，把“保护”变成替别人做决定 |\n| 初始目标 | 穿过封锁区，确认妹妹陆遥是否还活着 |\n| 长期目标 | 建立普通人也能活下去的安全秩序 |\n| 底线 | 不主动牺牲无辜者，不把同伴当消耗品 |\n| 核心矛盾 | 为了保护人类不断获得非人的力量 |\n\n三年前的一次仓库火灾中，陆沉服从错误命令撤退，没能救出被困同事，因此离开救援队。他对“该救谁、该放弃谁”异常敏感，这会成为末日中最锋利也最危险的软肋。\n",
      "source_selection": {
        "schema_version": "1.0",
        "policy": "story_project_source_paragraph_relevance_v1",
        "source_sha256": "a95f395f7022a11a4b555d924de2cee2190f90913853c2ee8bdc2bf046e3387a",
        "original_chars": 322,
        "selected_items": [
          {
            "id": "document",
            "sha256": "a95f395f7022a11a4b555d924de2cee2190f90913853c2ee8bdc2bf046e3387a",
            "original_chars": 322,
            "start_char": 0,
            "end_char": 322
          }
        ],
        "omitted_count": 0
      },
      "selection": {
        "schema_version": "1.0",
        "policy": "input_pack_story_source_paragraph_relevance_v1",
        "source_sha256": "a95f395f7022a11a4b555d924de2cee2190f90913853c2ee8bdc2bf046e3387a",
        "original_chars": 322,
        "selected_items": [
          {
            "id": "document",
            "sha256": "a95f395f7022a11a4b555d924de2cee2190f90913853c2ee8bdc2bf046e3387a",
            "original_chars": 322,
            "start_char": 0,
            "end_char": 322
          }
        ],
        "omitted_count": 0
      },
      "truncated": false
    }
  },
  "tracking_sources": {},
  "setting_sources": {},
  "source_paths": {
    "story_project_root": "D:\\Pycharm\\PythonProject\\NovelAgent\\books\\尸潮之下_我能提取进化词条_重写V2",
    "outline_path": "D:\\Pycharm\\PythonProject\\NovelAgent\\books\\尸潮之下_我能提取进化词条_重写V2\\大纲\\细纲_第026章.md",
    "previous_prose_path": "D:\\Pycharm\\PythonProject\\NovelAgent\\books\\尸潮之下_我能提取进化词条_重写V2\\正文\\第025章_第25章 黑旗名册.md",
    "tracking_paths": {
      "上下文.md": "D:\\Pycharm\\PythonProject\\NovelAgent\\books\\尸潮之下_我能提取进化词条_重写V2\\追踪\\上下文.md",
      "伏笔.md": "D:\\Pycharm\\PythonProject\\NovelAgent\\books\\尸潮之下_我能提取进化词条_重写V2\\追踪\\伏笔.md",
      "时间线.md": "D:\\Pycharm\\PythonProject\\NovelAgent\\books\\尸潮之下_我能提取进化词条_重写V2\\追踪\\时间线.md",
      "角色状态.md": "D:\\Pycharm\\PythonProject\\NovelAgent\\books\\尸潮之下_我能提取进化词条_重写V2\\追踪\\角色状态.md"
    },
    "setting_paths": {
      "作品设定.md": "D:\\Pycharm\\PythonProject\\NovelAgent\\books\\尸潮之下_我能提取进化词条_重写V2\\设定\\作品设定.md",
      "地点/临江市地下商超.md": "D:\\Pycharm\\PythonProject\\NovelAgent\\books\\尸潮之下_我能提取进化词条_重写V2\\设定\\地点\\临江市地下商超.md",
      "能力体系.md": "D:\\Pycharm\\PythonProject\\NovelAgent\\books\\尸潮之下_我能提取进化词条_重写V2\\设定\\能力体系.md",
      "角色/苏晴.md": "D:\\Pycharm\\PythonProject\\NovelAgent\\books\\尸潮之下_我能提取进化词条_重写V2\\设定\\角色\\苏晴.md",
      "角色/赵铁.md": "D:\\Pycharm\\PythonProject\\NovelAgent\\books\\尸潮之下_我能提取进化词条_重写V2\\设定\\角色\\赵铁.md",
      "角色/陆沉.md": "D:\\Pycharm\\PythonProject\\NovelAgent\\books\\尸潮之下_我能提取进化词条_重写V2\\设定\\角色\\陆沉.md"
    }
  },
  "source_resolution": {
    "precedence": [
      "current_outline_or_user_input",
      "tracking_state",
      "previous_prose",
      "settings",
      "snapshot_runtime_cache",
      "memory_sync",
      "model_inference"
    ],
    "entries": [
      {
        "field": "chapter_blueprint",
        "chosen_source": "current_outline",
        "discarded_sources": [],
        "reason": "current_outline_precedes_runtime_cache"
      },
      {
        "field": "tracking_context",
        "chosen_source": "tracking_files",
        "discarded_sources": [
          "snapshot.json",
          "memory",
          "model_inference"
        ],
        "reason": "tracking_state_precedes_snapshot_and_memory"
      },
      {
        "field": "previous_chapter",
        "chosen_source": "previous_prose",
        "discarded_sources": [
          "snapshot.json",
          "memory",
          "model_inference"
        ],
        "reason": "latest_prose_precedes_runtime_cache"
      },
      {
        "field": "setting_context",
        "chosen_source": "setting_files",
        "discarded_sources": [
          "snapshot.json",
          "memory",
          "model_inference"
        ],
        "reason": "settings_precede_runtime_cache"
      }
    ]
  }
}

# Requirements
- Advance the plot instead of restating setup.
- Preserve character, location, and timeline continuity from the Snapshot.
- If Project Profile sets a language, write the chapter only in that language.
- Continue directly from Story State and explain Spatial State transitions before changing locations.
- Treat Authoritative State stable IDs, aliases, relationships, rosters, counters, inventory, locations, and events as binding.
- Express every state change as a structured Scene delta; never infer a replacement identity or reset a counter.
- Introduce or intensify at least one concrete conflict.
- Treat Snapshot and Memory Index as read-only runtime context.
- If Recovery Context is available, address its problem codes and validation coverage gaps without contradicting the Snapshot.
- If StoryProject Chapter Blueprint is present, treat its required_beats and ending_pressure as mandatory chapter contract.
- Return only chapter prose, not analysis or JSON.
