# D2：格式、任务兼容与升级边界

日期：2026-09-09。状态：**T3.1 有限兼容试验通过；下一项 T7.0/D3。**

本决议确定格式边界和升级约束。新版状态的正式读取、提交与升级事务仍属于 T3.2；当前普通入口继续拒绝 ProjectIdentity 3.0，没有升级真实作品。

## 决议

1. 目标仍为 legacy Strict，不借此切换到 event authority。已有 ProjectIdentity 1.0/2.0 的磁盘格式保持不变；EA 保留现有行为和回归。
2. 新审核状态使用 **ProjectIdentity 3.0** 作为旧程序可识别的拒绝边界，沿用 book_id、created_at、Strict activation，root_hint 为 `.`。authority 保持 legacy，minimum_writer_contract 至少为 2。不能只提高最低契约数字。
3. 正式状态沿用 D1 的 `追踪/已审核事实.md` 受管块，语义格式定为 `reviewed_legacy_state_v1`。保留已确认初始基线、来源修订、静态引用、当前七类状态、已提交章游标及持久冲突决定。ProjectIdentity 3.0 必须明确绑定该格式与来源路径；升级凭证绑定旧身份文件哈希、基线确认和状态文件哈希。章节提交再由现有正式事务绑定更新后的状态、正文及游标。具体 schema 和事务实现由 T3.2 完成后才能启用，不把 D1 试验文件直接当正式数据。
4. `CURRENT_WRITER_CONTRACT` 本次仍为 1。读写尚未完成之前不声明支持契约 2，也不放开 identity schema 白名单。T3.2 必须同时启用 reader、writer、恢复核验及 schema，重新用实际 B0 验证最终完整格式。
5. 工作台任务使用独立语义契约 `workbench_execution_v1`，不使用 commit 字符串决定兼容。仅新任务写入该值；旧任务缺值或未知值均不可执行。T3.2 改变执行语义时必须升级任务契约并重新验证。

## 实际 B0 试验

B0 为 `090d0aa191f7aae96ea1c979b6725d7f87d811ae`。使用 `git archive` 提取的 758 个原始文件，运行前后全量哈希一致；没有改旧程序，也没有补丁替换其检查函数。归档 SHA-256：`226d705823984a6e7d2bb4864695cb9b5ff54d924461eec6536d61e0ccb8a45d`。

复现脚本：`scripts/experiments/run_d2_b0_compatibility.py`。最终 18 个案例及每个作品文件的 before/after 清单在：

`.tmp/stage-b/d2-b0-probes/9d3573b0bae3480582210db209fb1e01/report.json`

| 候选 | B0 实测 | 决定 |
|---|---|---|
| v2 + minimum=1 对照 | 初始化写入 snapshot/memory；对账创建锁；工作台成功创建任务 | 对照入口确实可运行 |
| v2 + minimum=2 | 初始化、对账和工作台仍成功；持久 dry-run 已产生运行凭证才因 Strict 写回要求退出 | 单独提高数字失败，不能作为防护 |
| v3 + minimum=2 | 初始化、持久 dry-run、对账、草稿恢复、锁章恢复、工作台导入/排队全因 identity schema 不支持而退出；作品文件零差异 | 采用外层 v3 格式边界 |

这里的 generation 是真实 CLI 的确定性持久 dry-run：B0 禁止 dry-run 与 apply writeback 组合，因此对照不证明正式章节提交成功。它明确证明检查发生在运行凭证写入之前还是之后；初始化成功写文件独立证明 minimum-only 方案失败。恢复对照无真实执行材料，不能据此宣称旧恢复完整成功；v3 的拒绝原因明确来自 schema。工作台试验使用实际 B0 的公开 Registry/JobStore，未启动 supervisor。

设计尝试为两种：minimum-only 失败，外层 v3 通过。中间脚本编码错误和参数错误记录保留，不伪装成兼容成功：早期 `.tmp/stage-b/d2-b0-probes/1c0206bf72b449408390812a980f3f36` generation 参数非法；`1ec80c9ac66b41c48f3d0f7cd68a1d6d` 为中间检查，不代表最终范围。

## 支持矩阵与入口检查

| 程序 / 数据 | 旧 identity 1/2、可支持契约 | reviewed identity 3 | 缺失/未知任务契约 |
|---|---|---|---|
| 实际 B0 | 原行为；legacy 最低契约检查有缺口 | 上述六条项目入口拒绝 | B0 不认识新增列，禁止把升级后的 DB 直接交给 B0 |
| 本次代码 | 保留旧行为；执行前统一检查最低契约 | 拒绝，尚未启用 | 查询保留；隔离调度、release、resume、恢复父任务与子进程执行 |
| T3.2 目标版本 | 保留旧路径，升级显式执行 | 完整 reader/writer/事务通过后支持 | 必须经明确的兼容评估；不自动给旧任务补当前契约 |
| 其他历史版本 | 无兼容保证 | 无兼容保证 | 无兼容保证 |

共享只读门为 `core/story_project/format_compatibility.py`。CLI 在路径解析后、所有提前分支与 runtime 写入前执行，包括 delivery/autonomy；工作台每章生成和恢复子进程前执行；候选 builder、实际 context runtime、managed writer 准备/应用和 `recover_locked_chapter` 直接调用同样检查。新版本对最低契约不足与未知格式均在这些入口写入前失败。只读 `load_project_identity` 仍允许检查已知 schema 的未来 minimum；未知 schema 只能查看原始文件/诊断，不能走正常项目接口。

保证针对正式带 StoryProject 身份的入口；不是针对任意历史程序、手工调用底层文件 API 或把运行目录脱离 StoryProject 身份后执行的通用文件写保护。不得把这个边界描述成文件系统权限防火墙。旧程序使用现场 DB 的回退限制必须遵守下文。

## 在途任务与升级预检

T7.1/T3.2 的升级执行器须按以下顺序落实，任何一项失败都不得发布新身份：

1. 取得本机实例/作品所有权，进入维护状态，禁止新任务和草稿编辑。已运行执行必须确实退出；仅更新 SQLite 状态不等于停止进程。
2. 核对未结算 Provider、未完成持久化事务和外部 runtime 根。含不确定调用只允许诊断归档，不能认证可恢复备份或自动重发调用。
3. 在停写期间通过 SQLite backup API 与受控文件快照取得目标作品恢复包。验证所有文件哈希、book_id、Strict 资格、D1 人工基线/静态引用/实体映射，出示范围及差异预览。
4. 列出 held/queued/starting/running/pause_requested/awaiting_input/needs_recovery 全部任务。原版本可完成的工作先完成；未完成旧任务的 config、result、error、事件和草稿保留，当前代码隔离执行。不能复制旧任务并补当前契约以规避检查。
5. 升级身份、状态文件和凭证须在单一正式事务完成，并检查准备到提交期间身份/正文/read-set 未变化。book_id、既有正文、不可变历史字节不得变化；所有旧准备计划作废。
6. 冷启动复核新格式、原始历史与当前状态一致，恢复实例默认零自动调用。按章节重新准备新任务；费用授权独立存在，升级不是调用授权。

本次实现的是第 4 项的执行隔离与入口保护，不声称第 1–6 项的真实升级执行器已经完成。`quarantine_incompatible_jobs` 遇到活动任务会拒绝；owned supervisor 启动先记录已退出旧进程的恢复不确定性，再隔离并跳过自动结果重分类。原 config/result/error 不因隔离被重写；隔离事件追加。API 返回 `execution_compatible=false`，网页显示版本不兼容并禁用恢复。

实际 B0 生成的 SQLite 文件已复制给新代码验证：旧排队任务保留字段和原事件，新增隔离事件、变为 needs_recovery，不能 claim/resume/release。来源 DB 哈希不变。证据：`.tmp/stage-b/d2-task-upgrade/5c03e8858e5b4a099cbb5a6bdae130f5/report.json`。脚本 `run_d2_task_upgrade.py` 使用实际 B0 数据，不仅是手造 NULL 单测。

## 回退条件

- 尚未升级作品也未继续写入：回到匹配代码与升级前 DB/作品快照；不要只替换代码并让 B0 读取新增任务列后的现场 DB。
- v3 已写入：禁止只回退代码、改 identity 版本号或移除新状态文件。B0 无法读新格式，这是预期保护。
- 必须回到 B0 时：保全升级后的完整诊断资料与新正文，再停写恢复**匹配 B0 的作品和 SQLite 快照**。升级后产物单独保留，不能直接覆盖旧状态。D3/T7.2 必须证明可写恢复与旧实例写入资格交接。
- 没有验证过的匹配快照、事务不确定或原进程仍存活：不执行回退/升级，保留现场并处理原因。

## 验证与限制

- D2 新增定向测试 28 通过；覆盖 CLI 五入口拒写与合法初始化、直接 context/recovery、两类 worker、NULL/未知任务四状态、旧数据库补列、活动任务拒绝隔离。
- 综合回归 144 通过、1 跳过、12 子测试通过：`.tmp/stage-b/d2-regression.xml`。
- 入口扩展回归先为 152 通过、63 子测试通过，1 项 mock 未设置路径属性失败；兼容无路径 mock 后，最终针对 autonomy/jobs/D2 的 69 通过、1 跳过、42 子测试通过：`.tmp/stage-b/d2-final-tests.xml`。首次失败记录不删除；未重跑全套。
- 浏览器结果通过：Chrome DevTools 实际点击书架→生产→待处理，刷新后仍显示旧任务隔离及禁用按钮。GET pending/job 为 200 且含不兼容字段；显式负向 POST resume 为 409，revision=2 与原记录未变。正常操作和刷新无控制台错误，负向请求仅有预期的 409 resource 提示。证据 `.tmp/stage-b/d2-browser/5fa7ab32e0fc41b4a916477d31dc3fee/browser-evidence.json`。隔离 worker 禁用；标签页和服务 PID1524 已停止。
- UI 首次验证曾误标“可恢复/模型状态不确定”，已修正为版本不兼容。草稿仍可查看与编辑保存；执行验证/确认继续受契约限制。
- 无 Provider 调用、无真实书籍写入。D2 不证明正式 v3 升级、两模式交付、真实模型质量或可写恢复；这些仍分别属于 T3.2、后续功能阶段和 D3/T7。
