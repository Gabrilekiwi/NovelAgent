# NovelAgent 2.0 接入 oh-story-claudecode：StoryProject 兼容方案

日期：2026-07-07

## 1. 关键修正

前一版把 `oh-story-claudecode` 误判为“可替换 OpenAI / Claude 调用的标准 API provider”。这是错误方向。

根据 `worldwonderer/oh-story-claudecode` 仓库实际内容，它是一个面向 Claude Code / OpenCode / OpenClaw / Codex CLI / workbuddy / 通用 Agent 环境的网文写作 skill 包。它的核心不是模型接口，而是：

- 13 个 story skills：setup、router、长篇写作、长篇拆文、长篇扫榜、短篇写作、短篇拆文、短篇扫榜、去 AI 味、导入小说、审稿、封面、browser-cdp。
- 7 个专业 agent：story-architect、character-designer、narrative-writer、consistency-checker、story-researcher、story-explorer、chapter-extractor。
- 多端部署资产：`.claude/agents`、`.opencode/agents`、`.codex/agents/*.toml`、`.codex/hooks.json`、`AGENTS.md`、本地 `skills/`。
- StoryProject 文件工程：`设定/`、`大纲/`、`正文/`、`追踪/`、`对标/`、`拆文库/`、`参考资料/`、`.active-book`。
- hooks 和确定性脚本：大纲守卫、session/compact 恢复、commit 提醒、AI 句式检查、退化检查、标点规范。

所以 NovelAgent 的接入目标应调整为：

> 以 StoryProject 文件工程为核心状态层，NovelAgent 作为结构化执行、校验、恢复和审计引擎嵌入；复用 oh-story 的 skill、agent、hook、脚本和文件协议，避免重复建设。

同时必须区分两个运行模式：

| 模式 | 含义 | 是否 v2.0 必需 |
| --- | --- | --- |
| StoryProject compatible mode | 只要有 `设定/`、`大纲/`、`正文/`、`追踪/`，NovelAgent 就能读取、生成、回写 | 必需 |
| oh-story enhanced mode | 如果检测到 `.story-deployed`、skills、hooks、agents，则提供兼容提示和可选复用 | 可选增强 |

`.story-deployed`、`.codex/hooks.json`、`.codex/agents/*.toml`、`AGENTS.md`、7 个 story agent 不能成为 NovelAgent 2.0 的核心前置条件。缺失时只能是 warning / info，不能让基础运行失败。

## 2. 当前 NovelAgent 能力定位

NovelAgent v1.5 当前健康基线：

- `python main.py --check --dry-run --memory data/notion_memory.example.json`：通过，20 项检查通过。
- `python -B -m unittest discover -s tests`：通过，392 个测试通过。

现有优势：

- Python schema 合同强，run record 可审计。
- Director / Executor / Validator / Repair / Recovery 链路完整。
- Notion/file memory writeback 已有质量门。
- preflight、provider smoke、unittest 基线稳定。

现有短板：

- 长期状态以 `snapshot.json` / memory JSON 为中心，不是网文作者可维护的文件工程。
- 没有一等的 `设定/大纲/正文/追踪/对标/拆文库`。
- 章节生成没有直接消费 oh-story 的细纲、对标节奏、情绪模块、文风、角色状态。
- 去 AI 味、正文前大纲守卫、写后退化检查等能力与 oh-story 已有能力重叠。

## 3. StoryProject 必须成为核心状态层

oh-story 最重要的抽象是 StoryProject：

```text
{书名}/
├── 设定/
│   ├── 世界观/
│   ├── 角色/
│   ├── 势力/
│   ├── 关系.md
│   └── 题材定位.md
├── 大纲/
│   ├── 大纲.md
│   ├── 卷纲_第X卷.md
│   └── 细纲_第XXX章.md
├── 正文/
│   └── 第XXX章_章名.md
├── 对标/
│   └── {对标书名}/
│       ├── 剧情/节奏.md
│       ├── 剧情/情绪模块.md
│       ├── 章节/
│       ├── 角色/
│       ├── 设定/
│       ├── 文风.md
│       └── 拆文报告.md
├── 拆文库/
│   └── {书名}/
├── 追踪/
│   ├── 上下文.md
│   ├── 伏笔.md
│   ├── 时间线.md
│   └── 角色状态.md
└── 参考资料/
```

建议重新定义 NovelAgent 的状态边界：

- **StoryProject 是源事实层**：设定、细纲、正文、追踪、对标、拆文资产以文件为准。
- **Snapshot 是运行缓存层**：NovelAgent 从 StoryProject 构建 runtime snapshot/input pack，但不再把 snapshot 当唯一长期真相。
- **RunRecord 是审计层**：继续保留 NovelAgent 的 schema run artifacts，用于记录一次生成、校验、修复、提交的证据。
- **Memory writeback 降级为同步层**：章节提交后优先更新 `追踪/伏笔.md`、`追踪/时间线.md`、`追踪/角色状态.md`、`追踪/上下文.md`，Notion/file memory 变成可选同步。

v2.0 的基础能力只要求 `设定/`、`大纲/`、`正文/`、`追踪/`。`对标/`、`拆文库/`、oh-story skills/hooks/agents 都是质量增强，不应阻塞基础生成。

### StoryProject 根目录命名

方案中统一使用 **StoryProject 根目录** 指代单本书的工程目录，也就是 `.active-book` 指向的目录，或 `--story-project PATH` 显式传入的目录。

规范：

- `workspace root`：NovelAgent 代码仓库根目录。
- `StoryProject root`：单本书工程目录，例如 `长篇/某本书/`。
- `active book`：`.active-book` 文件中记录的 StoryProject root 相对路径。

后续文档和代码命名统一使用：

- `story_project_root`
- `active_book_path`
- `StoryProjectRuntimeContext`

避免混用 `book_dir`、`project_dir`、`story_dir`、`书名目录` 等名称。

### oh-story 实际约定与 v2.0 兼容策略

对 `worldwonderer/oh-story-claudecode` 的 README、`story-long-write`、`story-import`、`story-setup` 模板和 hooks 核对后，不能把单一示例文件名当成唯一真实格式。v2.0 应采用“规范写入、兼容读取”的策略。

| 问题 | oh-story 实际状况 | NovelAgent v2.0 方案 |
| --- | --- | --- |
| 细纲文件名 | 文档中同时出现 `大纲/细纲_第XXX章.md`、`大纲/细纲_第N章.md`、`大纲/细纲_第N章*.md`。大纲守卫 hook 实际匹配 `细纲_第*章*.md`，并会去掉章节号前导 0。 | 规范写入 `大纲/细纲_第003章.md`。读取时接受 `细纲_第3章.md`、`细纲_第003章.md`、`细纲_第3章_*.md`、`细纲_第003章_*.md`。同一章多文件命中时 blocking，除非精确规范名存在且策略允许选择它并 warning。 |
| 正文文件名 | `story-import` 的标准化规则指向 `正文/第001章_章名.md`，无章名可变成 `第001章_无题.md`；hooks 实际接受 `正文/第*章*.md`，所以 `第003章.md` 也可能存在。 | 规范写入 `正文/第003章_章名.md`，章名从细纲 title 解析，缺失时用 `无题`。读取和冲突检测接受 `第3章.md`、`第003章.md`、`第3章_*.md`、`第003章_*.md`。同一章多个正文文件必须 blocking。 |
| `追踪/上下文.md` | `story-setup` 有模板，但它是 compact/session 恢复用的进度快照，包含当前位置、文风指纹、最近决策、待处理线索、写作变更、待办、已写摘要等标题；不是严格机器 schema。 | v2.0 只做标题级半结构化解析。缺失可按命令模式 blocking 或 high-risk warning；字段缺失不阻塞，记录 `missing_fields`。 |
| `追踪/角色状态.md` | `state-tracking` 和 `story-import` 给出的长篇格式相对稳定：`# 角色状态追踪`、`## 角色名`、当前身份、当前能力、关键关系、公众形象、待回收伏笔、状态变更记录。短篇不一定有该文件。 | 长篇模式按该格式优先解析；缺失 high-risk warning，不默认 blocking。解析失败时保留原文进入 memory/context overlay，并记录 warning。 |
| `追踪/伏笔.md` | hooks 会读取表格并检查状态列，常见状态是 `未埋`、`已埋`、`已回收`，但列名和扩展字段可能变化。 | 使用宽松 Markdown table parser，只依赖可识别的伏笔内容、状态、章节字段。格式不符 warning，不阻塞基础生成。 |
| `.active-book` | 真实存在。`story-setup` 的 common hook 读取第一行，trim 空白，把它解析为 StoryProject root；相对路径和绝对路径都可处理。缺失时 hooks 会 fallback 到首个含 `追踪/`、`正文/` 或 `正文.md` 的目录。 | 支持 `.active-book`，但不要求存在。优先级：`--story-project PATH` > `.active-book` > `--story-project auto` 目录发现。run record 记录实际来源。 |
| `story-setup` 生成目录 | `story-setup` 主要部署 skills/hooks/agents/AGENTS/sentinel，并不保证每次创建完整 StoryProject；它会保守合并或跳过已有文件。完整书籍结构更多来自 `story-long-write` 或 `story-import`。 | 不把 `story-setup` 当 StoryProject 完整性来源。v2.0 独立做 `story_project_structure` 校验，只把 `story-setup` 状态放进 `oh_story_installation` info。 |
| Codex / Claude Code / OpenCode 差异 | StoryProject 目标结构基本一致；差异主要在 `.codex/`、`.claude/`、`.opencode/` 的 agents、hooks、commands、AGENTS 模板。部分模板列出的追踪文件不完全一样。 | v2.0 忽略 CLI 部署层差异，只识别 StoryProject compatible structure。CLI 相关检测后置到 enhanced mode。 |

因此 v2.0 需要一个独立的 filename resolver，而不是在业务代码里散落 `glob("细纲_第XXX章.md")` 这类硬编码：

- `resolve_outline(chapter_index)`：返回唯一细纲路径、候选列表和冲突原因。
- `resolve_prose(chapter_index)`：返回已有正文路径列表，用于 auto 定位和写入前冲突检测。
- `canonical_outline_path(chapter_index)`：只用于 NovelAgent 新建文件。
- `canonical_prose_path(chapter_index, title)`：只用于 NovelAgent 写正文。

### Source Precedence

StoryProject 成为源事实层后，必须定义冲突解决规则。否则 `StoryProject`、`snapshot.json`、Notion memory、上一章正文和模型推断会互相打架。

同一事实冲突时，默认优先级如下：

1. 当前章节细纲 / 用户显式输入。
2. `追踪/角色状态.md`、`追踪/上下文.md`。
3. 最新正文章节。
4. `设定/角色/*.md`、`设定/世界观/*.md`、`设定/关系.md`。
5. `snapshot.json` runtime cache。
6. Notion/file memory sync。
7. 模型推断。

冲突不能静默覆盖。映射阶段必须记录 source resolution，并写入 run record，例如：

```json
{
  "source_resolution": [
    {
      "field": "character.location",
      "chosen_source": "追踪/角色状态.md",
      "discarded_sources": ["snapshot.json", "memory"],
      "reason": "tracking_state_precedes_runtime_cache"
    }
  ]
}
```

## 4. 避免重复建设

不应在 NovelAgent 中重写这些 oh-story 已有能力：

| 能力 | oh-story 已有机制 | NovelAgent 应做什么 |
| --- | --- | --- |
| 写作工程目录 | StoryProject 协议 | 读取、校验、回写该协议 |
| 环境部署 | `story-setup` | 检测部署状态，必要时提示用户运行 setup |
| Codex agents/hooks | `.codex/agents`、`.codex/hooks.json` | merge 兼容，不覆盖 |
| 大纲守卫 | `guard-outline-before-prose` hook | 与其兼容，只补 preflight 检查 |
| 去 AI 味 | `story-deslop` | 复用脚本和 skill，不重写规则库 |
| 拆文/对标 | `story-long-analyze`、`story-import` | 消费 `拆文库/` 和 `对标/` 产物 |
| 多 agent 分工 | 7 个专业 agent | 复用，不复制 prompt 到 Python |

NovelAgent 应保留和增强：

- schema 化运行审计。
- deterministic validator。
- repair budget / recovery planning。
- StoryProject -> snapshot/input pack 的可追踪转换。
- run commit -> StoryProject 追踪文件的结构化回写。
- 对 StoryProject 完整性做机器可验证检查。

## 5. 推荐架构

```text
oh-story skills / agents / hooks
        |
        v
StoryProject 文件工程  <--------------------+
        |                                   |
        | build runtime context             | commit writeback
        v                                   |
NovelAgent StoryProject Adapter             |
        |                                   |
        v                                   |
Snapshot / Input Pack / Director / Engine --+
        |
        v
RunRecord / Pipeline Artifacts / Reports
```

新增模块建议：

```text
core/story_project/
├── __init__.py
├── paths.py              # .active-book、书目目录、标准子目录发现
├── model.py              # StoryProjectRuntimeContext 等轻量结构
├── loader.py             # 读文件
├── mapper.py             # StoryProject -> snapshot / memory / chapter_blueprint
├── writer.py             # run result -> StoryProject files
├── validator.py          # StoryProject 完整性校验
└── oh_story_compat.py    # 可选检测 .story-deployed、skills、agents、hooks 状态
```

这不是 provider adapter，而是 project substrate adapter。

不要命名为 `builder.py`。当前项目已有 `core/state/builder.py` 和 Snapshot Builder 概念，`story_project/builder.py` 会制造概念冲突。

职责划分：

- `loader.py`：只读文件，解析目录。
- `mapper.py`：把 StoryProject 映射为 runtime 输入，包括 `snapshot_overlay`、`memory_context_overlay`、`chapter_blueprint`、`source_paths`、`source_resolution`。
- `writer.py`：把提交结果回写到 `正文/` 和 `追踪/`。
- `validator.py`：检查 StoryProject 是否足够支持本次运行。
- `oh_story_compat.py`：只做增强检测，不影响基础运行。

## 6. 分阶段实施

### Phase 0：StoryProject compatible baseline

目标：只支持 StoryProject 风格目录，不要求 oh-story 安装。

交付：

- `core/story_project/paths.py`
- `core/story_project/loader.py`
- `core/story_project/validator.py`
- `main.py --story-project auto|PATH`
- `main.py --chapter N|auto`
- preflight `story_project_structure`

核心检查：

| 检查 | v2.0 等级 |
| --- | --- |
| active book 路径不存在 | blocking |
| 缺 `设定/`、`大纲/`、`正文/`、`追踪/` | blocking |
| 本章缺可解析细纲文件 | blocking |
| `追踪/上下文.md` 缺失 | blocking 或 high-risk warning，按命令模式决定 |
| `追踪/角色状态.md` 缺失 | high-risk warning |
| 正文章节号冲突 / 目标章节文件已存在 | blocking，除非用户选择覆盖/修订模式 |

验收：

- 有 `设定/大纲/正文/追踪` 即可运行 preflight。
- 缺 `.story-deployed` 不阻塞。
- 缺 `.codex/hooks.json` 不阻塞。
- 原 `--memory` JSON 路径保持兼容。

#### 章节定位规则：`--chapter N|auto`

StoryProject 模式必须显式定义本次运行要处理哪一章。

CLI：

```bash
python main.py --story-project auto --chapter auto
python main.py --story-project auto --chapter 21
```

规则：

| 参数 | 行为 |
| --- | --- |
| `--chapter N` | 处理第 N 章。必须通过 filename resolver 解析到唯一细纲；如果已存在任意 `正文/第N章*.md` 兼容命中文件，默认 blocking，除非后续进入修订模式。 |
| `--chapter auto` | 自动定位下一章：扫描 `正文/第N章*.md` 兼容命中的最大连续章节号，取 `N+1`；若 `正文/` 为空，则取第 1 章。 |
| 未传 `--chapter` | StoryProject 模式下等同于 `auto`，但 preflight summary 要显示 resolved chapter。 |

`auto` 定位必须同时检查：

- 可解析到唯一细纲。规范名是 `大纲/细纲_第NNN章.md`，但读取兼容 `细纲_第N章*.md`。
- 不存在任何兼容命中的目标正文文件。规范写入名是 `正文/第NNN章_章名.md`，但冲突检测兼容 `第N章*.md`。
- 如果 `追踪/上下文.md` 声明的最后完成章节与 `正文/` 扫描结果冲突，按 Source Precedence 记录 source resolution，并给 high-risk warning。

run record 必须记录：

```json
{
  "story_project": {
    "chapter_resolution": {
      "requested": "auto",
      "resolved_chapter": 21,
      "basis": ["正文/", "追踪/上下文.md"],
      "warnings": []
    }
  }
}
```

### Phase 1：StoryProject -> Runtime 映射

目标：把 StoryProject 映射成现有 NovelAgent runtime 能消费的输入。

交付：

- `core/story_project/mapper.py`
- `StoryProjectRuntimeContext`
- source precedence
- source resolution run record 字段

映射输出：

- `snapshot_overlay`
- `memory_context_overlay`
- `chapter_blueprint`
- `source_paths`
- `source_resolution`

#### `chapter_blueprint` schema

`chapter_blueprint` 是 StoryProject 细纲进入 NovelAgent Chapter Pipeline 的稳定合同，不能只把细纲原文拼进 prompt。

建议新增 schema：

```text
schemas/chapter_blueprint.schema.json
```

最小字段：

```json
{
  "chapter_index": 21,
  "outline_path": "长篇/某本书/大纲/细纲_第021章.md",
  "title": "章名",
  "word_target": 3000,
  "target_emotion": "爽感释放",
  "position": "推进",
  "core_event": "一句话核心事件",
  "opening_hook": "章首钩子",
  "ending_hook": "章尾钩子",
  "ending_pressure": "下一章推动力或未解决压力",
  "required_beats": [
    {
      "index": 1,
      "text": "谁做了什么 + 功能标签",
      "function": "铺垫",
      "density": "dense",
      "budget_chars": 250
    }
  ],
  "characters_in_order": ["角色A", "角色B"],
  "relationship_changes": [
    {
      "character": "角色A",
      "before": "本章前",
      "after": "本章后"
    }
  ],
  "plot_threads": {
    "main": "主线推进",
    "secondary": "辅线推进",
    "event": "事件线 / 任务线",
    "relationship": "感情线 / 关系线",
    "logic": "原因 -> 行动 -> 结果 -> 后果/新问题"
  },
  "source_path": "长篇/某本书/大纲/细纲_第021章.md",
  "missing_fields": []
}
```

字段等级：

| 字段 | v2.0 要求 |
| --- | --- |
| `chapter_index` | required |
| `outline_path` / `source_path` | required |
| `title` | required，可从细纲标题或文件名解析 |
| `core_event` | required |
| `required_beats` | required，至少 1 条；缺失 blocking |
| `ending_pressure` | required；缺失 high-risk warning，可从章尾钩子/结尾设定降级推断 |
| `word_target` | optional，缺失用默认配置 |
| `target_emotion` / `position` | optional，缺失 warning |
| `characters_in_order` / `relationship_changes` / `plot_threads` | optional，缺失 warning |

`chapter_blueprint` 必须通过 schema 校验后才能进入 pipeline。旧版细纲字段不完整时，mapper 可以生成 `missing_fields`，但不得凭空补剧情事实。

#### run record schema 更新

建议更新 `schemas/run_record.schema.json`，新增 `story_project` 段：

```json
{
  "story_project": {
    "enabled": true,
    "mode": "compatible",
    "root": "长篇/某本书",
    "active_book_path": ".active-book",
    "chapter_resolution": {
      "requested": "auto",
      "resolved_chapter": 21,
      "basis": ["正文/", "追踪/上下文.md"],
      "warnings": []
    },
    "source_paths": {
      "outline": "长篇/某本书/大纲/细纲_第021章.md",
      "previous_chapter": "长篇/某本书/正文/第020章_章名.md",
      "tracking_context": "长篇/某本书/追踪/上下文.md",
      "character_state": "长篇/某本书/追踪/角色状态.md",
      "foreshadowing": "长篇/某本书/追踪/伏笔.md",
      "timeline": "长篇/某本书/追踪/时间线.md"
    },
    "chapter_blueprint": {},
    "source_resolution": [],
    "writeback": {
      "attempted": false,
      "applied": false,
      "targets": [],
      "blocked_reasons": []
    }
  }
}
```

兼容要求：

- 非 StoryProject 模式下 `story_project.enabled=false` 或该段可为空，具体采用 schema 里的 nullable 设计。
- `run_result.schema.json`、report、preflight history loader 需要同步识别该字段。
- schema consistency tests 要覆盖 `chapter_blueprint.schema.json` 和 run record 嵌套块。

运行边界：

```text
main.py / runtime factory
  -> load StoryProject
  -> map to StoryProjectRuntimeContext
  -> materialize snapshot + memory_context overlays
  -> AgentExecutor consumes runtime inputs
```

`AgentExecutor` 不直接负责找 `.active-book`、读 `设定/大纲/追踪`、解析对标文件或写 StoryProject 文件路径。

### Phase 2：执行生成和回写

目标：完成基础闭环。

交付：

- runtime factory 组装 snapshot/memory overlays。
- input pack 增加 StoryProject sections：
  - 当前细纲。
  - 上一章正文或摘要。
  - 相关角色状态。
  - 活跃伏笔。
  - 当前上下文。
- `writer.py` 回写：
  - `正文/第XXX章_章名.md`
  - `追踪/上下文.md`
  - `追踪/伏笔.md`
  - `追踪/时间线.md`
  - `追踪/角色状态.md`

保护：

- rejected / failed run 不回写正文和追踪。
- 如果目标正文文件已存在，默认阻塞，除非进入修订模式。
- 回写前保存 run artifact 和 diff summary。

#### Chapter Pipeline 消费约束

Chapter Pipeline 不能只“参考”细纲文本，而必须消费 `chapter_blueprint.required_beats` 和 `chapter_blueprint.ending_pressure`。

要求：

- `plan_chapter`：
  - StoryProject 模式下不得让模型重新发明章节计划。
  - 应以 `chapter_blueprint.required_beats` 为章节计划主骨架。
  - 可以把 beats 分组为 scenes，但不能删除 required beat。
- `generate_scenes`：
  - 每个 scene 必须携带要覆盖的 beat indexes。
  - prompt 中明确列出该 scene 的 required beats。
  - scene draft 返回后记录 covered beat indexes。
- `merge_scenes`：
  - 合并后检查所有 required beats 至少被覆盖一次。
  - 未覆盖 required beat 时，validation 增加 blocking problem，例如 `missing_required_beat`。
- `ending_pressure`：
  - 章尾必须体现 `ending_pressure` 或明确等价的下一章推动力。
  - 缺失时 validation 增加 problem，例如 `missing_ending_pressure`。

run record / pipeline artifact 应记录：

```json
{
  "chapter": {
    "pipeline": {
      "blueprint_coverage": {
        "required_beat_count": 10,
        "covered_beat_indexes": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        "missing_beat_indexes": [],
        "ending_pressure_required": true,
        "ending_pressure_observed": true
      }
    }
  }
}
```

这条约束是 v2.0 的核心之一：StoryProject 模式下，细纲不是提示素材，而是 pipeline contract。

### Phase 3：oh-story enhanced detection

目标：检测 oh-story 安装状态，但只做增强提示。

交付：

- `core/story_project/oh_story_compat.py`
- preflight `oh_story_installation`
- 报告 `.story-deployed`、skills、hooks、agents 状态。

这些检查只能 info / warning，不能阻塞基础运行：

| 检查 | 等级 |
| --- | --- |
| `.story-deployed` 缺失 | info |
| `skills/story-setup` 缺失 | info |
| `.codex/agents/*.toml` 缺失 | info |
| `.codex/hooks.json` 缺失 | info |
| `story_codex_hook.py` 缺失 | info |
| `AGENTS.md` 无 story routing | info |
| 7 个 story agent 不完整 | info |

不做：

- 不修改 `.codex/`。
- 不自动安装 skills。
- 不执行 JS 脚本。

### Phase 4：可选质量脚本

目标：谨慎复用 oh-story 确定性脚本。

涉及脚本：

- `check-ai-patterns.js`
- `check-degeneration.js`
- `normalize-punctuation.js`

v2.0 策略：

| 行为 | 默认 |
| --- | --- |
| 检测脚本是否存在 | 默认执行，只读检查 |
| 执行脚本 | 默认不执行 |
| 开启脚本执行 | `--story-quality-scripts` |
| 脚本失败是否阻塞 | 默认不阻塞 |
| 要求脚本成功 | `--require-story-quality-scripts` |

执行约束：

- 只允许白名单脚本。
- 固定 cwd 在当前 workspace 或 active book 内。
- 不通过 shell 拼接命令。
- 设置 timeout。
- 输入路径必须解析后仍位于 StoryProject / workspace 内。
- 默认只读检查；涉及 `normalize-punctuation.js` 这种会改文件的脚本，必须显式启用并在 run record 记录修改。

建议版本边界：

- v2.0：只检测脚本存在和可配置性。
- v2.1：可选执行只读检查脚本。
- v2.2：可选执行修复脚本，并纳入可审计 artifact。

### Phase 5：Codex / oh-story 深度增强

后置能力：

- `.codex/hooks.json` merge 辅助。
- story agents 检测和使用建议。
- `story-import` / `story-long-analyze` / `story-review` 流程协同。
- story-explorer / consistency-checker / narrative-writer 作为可选外部协作能力。

这些不属于 v2.0 必做范围。

## 7. 对标资产的 v2.0 等级

对标资产属于质量增强，不应阻塞 v2.0 基础生成。

| 文件 | v2.0 等级 |
| --- | --- |
| `对标/{书}/剧情/节奏.md` | warning |
| `对标/{书}/剧情/情绪模块.md` | warning |
| `对标/{书}/文风.md` | warning |
| `拆文库/{书}/` | info / warning |

v2.0 最关键的是：

- 能读 StoryProject。
- 能消费当前章节细纲。
- 能写正文。
- 能更新追踪。

对标节奏、情绪模块、文风、去 AI 味适合放入 v2.1 / v2.2 增强。

## 8. 从当前 NovelAgent 迁移的路线

当前已有 `data/snapshot.example.json` 和 `data/notion_memory.example.json`。建议做一次显式迁移，而不是直接废弃：

1. 新建或指定 active book：

```text
长篇/{书名}/
```

2. 从 snapshot/memory 生成初始 StoryProject：

```text
设定/世界观/*.md
设定/角色/*.md
设定/关系.md
大纲/大纲.md
追踪/伏笔.md
追踪/时间线.md
追踪/角色状态.md
追踪/上下文.md
```

3. 写 `.active-book` 指向该目录。

4. 后续推荐从 StoryProject 读取：

```bash
python main.py --story-project auto
```

5. 保留原 JSON 模式作为兼容：

```bash
python main.py --memory data/notion_memory.example.json
```

## 9. 不推荐做的事

- 不把 oh-story 当标准 API provider。
- 不新增 `api/oh_story_claudecode_client.py` 作为主路线。
- 不把 7 个 story agent prompt 翻译成 Python 模块。
- 不重写 `story-deslop` 的禁用词、去 AI 规则、退化检测。
- 不另造一套 `project_profile` 文件结构替代 `设定/大纲/正文/追踪/对标/拆文库`。
- 不让 Notion memory 继续压过 StoryProject 成为长期事实层。
- 不假设未 trust 的 `.codex/` custom agents 一定可用。
- 不要求 `.story-deployed` 或 `.codex/hooks.json` 存在才允许 NovelAgent 运行。
- 不在 v2.0 默认执行 oh-story JS 脚本。
- 不把对标文件缺失设为 blocking。
- 不把 StoryProject 文件读取职责塞进 `AgentExecutor`。

## 10. 最小实施清单

v2.0 必做：

- [ ] 新增 `core/story_project/paths.py`。
- [ ] 新增 `core/story_project/loader.py`。
- [ ] 新增 `core/story_project/validator.py`。
- [ ] 新增 `core/story_project/mapper.py`。
- [ ] 实现 StoryProject filename resolver：规范写入 `细纲_第NNN章.md` / `第NNN章_章名.md`，兼容读取 `第N章*.md`。
- [ ] 新增 `StoryProjectRuntimeContext`。
- [ ] 实现 source precedence / source resolution。
- [ ] 实现 `--chapter N|auto` 章节定位。
- [ ] 新增 `schemas/chapter_blueprint.schema.json`。
- [ ] 更新 `schemas/run_record.schema.json` / `schemas/run_result.schema.json` 的 StoryProject 段。
- [ ] `main.py` 增加 `--story-project` 参数。
- [ ] `run_preflight()` 增加 `story_project_structure` 检查。
- [ ] runtime factory 组装 snapshot/memory overlays。
- [ ] run record 记录 StoryProject source paths。
- [ ] Chapter Pipeline 消费 `required_beats` / `ending_pressure` 并记录 coverage。
- [ ] 章节提交回写 `正文/` 和 `追踪/`。
- [ ] 与现有 writeback gate 对齐，防止 rejected run 污染工程文件。
- [ ] 单测覆盖：缺目录、缺细纲、细纲别名、多细纲冲突、正文别名、多正文冲突、`.active-book` 首行解析、缺上下文、缺角色状态、冲突源优先级、`--chapter auto`、目标正文已存在、required beat 缺失、ending pressure 缺失。

v2.0 不做：

- [ ] 默认执行 oh-story JS 脚本。
- [ ] 修改 `.codex/hooks.json`。
- [ ] 要求 `.story-deployed`。
- [ ] 要求 7 个 story agents。
- [ ] 把对标文件缺失设为 blocking。
- [ ] 把 StoryProject 文件读取塞进 `AgentExecutor`。

## 11. 验收标准

接入成功的标准不是“能调用 oh-story”，而是：

- NovelAgent 可以把 StoryProject 当主要状态源。
- 没有 `.story-deployed` 时仍可运行 StoryProject compatible mode。
- NovelAgent 不破坏 oh-story 的目录结构、hooks、agents、skills。
- NovelAgent 新写文件使用规范命名，但能读取 oh-story hooks 接受的宽松文件名。
- run record 能追踪本章使用了哪些细纲、角色状态、伏笔和 source resolution。
- 章节提交后，StoryProject 的 `正文/` 和 `追踪/` 被一致更新。
- 缺细纲 blocking；缺角色状态 high-risk warning；缺对标资产 warning。
- Executor 不直接读取 StoryProject 路径。
- `--chapter auto` 能稳定解析下一章，并在 run record 记录 chapter resolution。
- `.active-book` 存在时按第一行路径解析；缺失时不阻塞，可走显式路径或 auto discovery。
- `chapter_blueprint` 通过 schema 校验后才进入 Chapter Pipeline。
- Chapter Pipeline 覆盖所有 `required_beats`，并校验 `ending_pressure`。
- 现有 dry-run、unittest、preflight 不回归。

必跑命令：

```bash
python main.py --check --dry-run --memory data/notion_memory.example.json
python -B -m unittest discover -s tests
python -B scripts/smoke_v1.py
```

新增后补充：

```bash
python main.py --check --story-project auto
python main.py --dry-run --story-project auto
```

## 12. 参考来源

- GitHub 仓库：`https://github.com/worldwonderer/oh-story-claudecode`
- `README.md`：安装方式、13 个 skills、7 个 agents、hooks、StoryProject 文件结构、Codex 部署说明。
- `skills/story-setup/SKILL.md`：部署协议、`.codex/agents`、`.codex/hooks.json`、`.story-deployed`、merge 策略。
- `skills/story-long-write/SKILL.md`：长篇 StoryProject 结构、日更流程、对标召回、追踪文件维护。
- `skills/story-import/SKILL.md`：已有小说反向构建 StoryProject，`拆文库/` 与 `对标/` 的关系。
- `skills/story-deslop/SKILL.md`：去 AI 味流程和本地确定性脚本。
- `skills/story-import/references/structure-mapping-long.md`：正文标准化命名、拆文库到 StoryProject 的映射。
- `skills/story-setup/references/templates/hooks/lib/common.sh`：`.active-book` 解析和 fallback discovery。
- `skills/story-setup/references/templates/hooks/guard-outline-before-prose.sh`：大纲和正文文件名的 hook 兼容匹配规则。
- `skills/story-setup/references/templates/hooks/detect-story-gaps.sh`：`追踪/伏笔.md` 状态检查和长短篇目录发现。
- `skills/story-setup/references/templates/上下文.md.tmpl`：`追踪/上下文.md` 模板。
- `skills/story-long-write/references/state-tracking.md`：`追踪/角色状态.md` 长篇维护格式。
