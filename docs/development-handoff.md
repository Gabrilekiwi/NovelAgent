# NovelAgent 开发交接

更新时间：2026-09-09。

## 当前任务

阶段A已完成并提交33a0944。阶段B的T2.1已提交fd85291，T8.0已提交52699d0，D1完整上下文桥接已提交d33deff。T3.1/D2有限兼容试验通过：实际B0验证v3拒写，当前版本统一执行门与旧任务隔离、浏览器提示均已验证。下一步T7.0/D3可写恢复。最新HEAD以Git为准；完整开发目标仍未完成。

- 唯一主计划：[最终执行版1.0](D:/Pycharm/PythonProject/NovelAgent/docs/development-plan-final-2026-09-08.md)
- 复审证据：[开发计划复审](D:/Pycharm/PythonProject/NovelAgent/docs/development-plan-review-2026-09-08.md)
- 历史原案：[原开发计划](D:/Pycharm/PythonProject/NovelAgent/docs/development-plan-2026-09-08.md)
- 审查依据：[2026-09-07审查](D:/Pycharm/PythonProject/NovelAgent/docs/project-review-2026-09-07.md)
- 审查原始交接：[HANDOFF](D:/Pycharm/PythonProject/NovelAgent/.tmp/project-review-20260907/HANDOFF.md)

## 已锁定的用户选择

1. 稳定自用版，Windows本地单用户。
2. 逐章审核与连续自动两种模式同时交付。
3. 自动模式允许一次预算内自动修复，未解决则暂停；不确定调用不能自动重发。
4. 首版先覆盖已有作品，完整从零建书后置。
5. 真实模型和阅读验证独立安排，尚未授权新的付费调用或确定金额。

## 当前代码与数据

- 当前分支`codex/stable-self-use`；B0代码提交`090d0aa191f7aae96ea1c979b6725d7f87d811ae`，父提交`fbf6b8f`。原有43个代码/schema/测试/UI文件已保存；基线文件清单与哈希见`docs/development/b0-manifest.json`，原始文件归档在`.tmp/development-b0`。后续HEAD以Git实际状态为准。
- 目标书为`books/尸潮之下_我能提取进化词条_重写V2`，上次审查正式正文26章；开始实现时重新核对最新状态。
- 目标身份是legacy Strict，未迁移到event authority。最终版在目标旧书上交付两模式；EA保留现有行为和相关回归，新流程完整对等后置。不能把“两种模式”误读成“两后端同时完整对等”。
- 重要细节：旧Memory提交调用`_chapter_operations(... include_authoritative_state=False)`，原managed writer与fact context也未覆盖完整七类事实。T3必须补齐保存和冷启动读取，不能只传一个delta就认为兼容。
- 自动策略接受必须区别于作者确认；不能复用`author_confirmed=true`伪造人工审核。
- 旧semantic parser会跳过已有人工authority字段，受管值仅为supporting。T3必须先验证人工基线、章节变化与当前状态的来源规则，不能只增加持久化字段。
- 当前remap只支持保持目录身份的同卷rename，不能冒充解压恢复。旧提交path guard包含device/inode目录身份；T7需先验证恢复证明与可写恢复路径，不能仅重绑定路径。
- T1数据目录锁已通过执行进程继承覆盖父进程强制结束时子进程的存活情况；同机跨数据目录作品副本交接仍留给T7，不能混同。

## 已完成验证

2026-09-07审查基线：1905 tests、561 subtests通过；8跳过；本地smoke通过。日志在`.tmp/project-review-20260907`。9月8日计划制定与复审只做定向只读检查及文档更新，没有重跑全套测试，也没有模型调用、产品代码实现或真实书籍写入。最新恢复/子进程风险是代码审查结论，尚未执行新的故障注入。

9月8日阶段A实施：35项锁定依赖检查和pip check通过；项目环境相关测试最初101通过、1跳过、2子测试通过，1项启动器混合换行失败，已修复。修复后启动器2项通过；全新中文/空格路径环境的工作台app测试18项通过。项目环境smoke通过，21/21预检、隔离提交与报告通过，无Provider调用。详情和原始证据见`docs/development/stage-a-progress.json`。未执行远端CI，未声称全套测试本轮重新通过。

## 工作包状态

| 编号 | 状态 | 下一步 |
|---|---|---|
| T0.1 | 已完成 | B0代码提交、48文件原始归档和哈希清单已核验 |
| T0.2 | 已完成 | 35项依赖锁、项目环境、全新安装、启动器、文档与CI安装入口已完成本地验证；远端CI未运行 |
| T1.1 | 已完成 | 应用初始化前取得数据目录所有权，继承锁覆盖执行子进程；并发、端口占用、父死子活、监控异常验证证据在阶段A目录 |
| T1.2 | 已完成 | 不存在父目录400中文提示，表单保留；改正目录201，刷新仍可读取作品 |
| T2.1 | 已完成 | 七类共享schema、模型完整例子、纯准备计算与拒绝场景；见阶段B证据 |
| T3.0 | 有限试验完成 | D1来源/冷读决议通过；普通入口未启用新格式，正式事务仍待T3.2 |
| T3.1 | 有限试验完成 | D2实际B0六入口拒绝v3、旧任务隔离、升级预检与回退边界；正式v3读写仍待T3.2 |
| T2其余、T3–T7 | 范围与子任务已确定、待实现 | 按最终版阶段A–F推进；D1旧书来源、D2格式兼容、D3可写恢复先做有限试验 |
| T8.0 | 已完成材料准备与离线运行 | 12项版本化样例、3份真实材料只读快照；4项语义缺口保留给T2.2/T2.4；没有人工质量评分或新模型效果评估 |
| T8.1、T8.2 | 独立阶段、未授权真实调用 | 早期逐章配对与最终两模式连续验收分别限定预算；缺授权可继续独立工程 |

隔离浏览器服务PID34396与session66437已停止，隔离标签页已关闭。工作台回归session87351已退出，测试证据文件在`.tmp/stage-a`。系统中原有pythonw进程15004未操作，不应把它视为本任务创建的服务。续接时仍需核对实际进程与Git状态。

## 下一条工作

下一步T7.0/D3：在隔离目录准备一个经正式事务提交的章和一个SQLite作者草稿，复制到同机新空目录，核验旧不可变历史字节不变；调查旧path guard的device/inode约束，建立有来源证明的新物理绑定。用假Provider在新进程提交下一章，并验证原实例/恢复实例同book_id写入资格交接。不能用只读解压或改旧manifest/hash代替。先保存有限试验设计，再实施；实际备份产品入口属于T7.1/T7.2。保持真实书只读和Provider零调用。

D2决议详见`docs/development/d2-format-compatibility.md`。采用ProjectIdentity 3.0 + reviewed_legacy_state_v1边界；minimum-only方案已由实际B0写runtime证明失败。当前schema仍只支持1/2，CURRENT_WRITER_CONTRACT仍为1；T3.2完整reader/writer/事务通过后才可启用3/2。不可现在改真实identity或直接发现D1试验文件。

B0未修改归档`.tmp/stage-b/d2-b0/0dac6b9cf1184e7b82e4dce979439155`，758文件哈希运行前后一致。最终18例报告`.tmp/stage-b/d2-b0-probes/9d3573b0bae3480582210db209fb1e01/report.json`；实际B0数据库升级隔离报告`.tmp/stage-b/d2-task-upgrade/5c03e8858e5b4a099cbb5a6bdae130f5/report.json`。不重复解压/重复D1调查。

D2统一门覆盖main所有提前分支、candidate builder、runtime/writer、locked recovery和worker。JobStore新增nullable execution_contract列，不回填旧任务；旧契约任务仍可查询，禁止claim/resume/release/恢复父任务及子进程执行，旧恢复凭证不能解锁当前任务。启动持有实例所有权后才分类旧活动任务；代码回退须匹配作品与DB快照。网页已修正旧任务被误标为可恢复的问题。

D2测试：28新增通过；综合144通过/1跳过/12子测试，入口扩展152通过/63子测试加1个mock失败，修复后最后69通过/1跳过/42子测试；文件名及哈希在stage-b-progress。浏览器正常页无控制台异常、负向resume409且不改revision；只运行隔离书，worker禁用。服务session68891/PID1524和Chrome页面3均已停止。测试session49713已退出。旧无关pythonw进程15004不要操作。

D1最终读取路径：内部显式注入`load_runtime_view`到`ChapterCandidateContextBuilder`；`core/story_project/reviewed_state_context.py`提供绑定视图、当前snapshot及隔离Memory；`build_chapter_fact_context`使用同一视图。默认构造器不会发现试验文件，EA拒绝携带legacy视图。`core/state/location_mirrors.py`约束冗余人物位置，fact prepare会随位置事实同步已有镜像；保留来源等级。静态人物字段只做唯一显式姓名/别名匹配，诊断未映射；结构化静态空间/伏笔和绑定原文的静态片段进入prompt。原人工内容保留在项目/context供审计。

D1最终证据目录`.tmp/stage-b/d1-attempt-1/7304d18d4aea4ae1af7de44d19caf6a0`，三个新进程检查实际Memory伪造缓存/删除缓存/持久冲突决定。测试108通过+4子测试，最后EA拒绝检查后41通过；XML分别为`.tmp/stage-b/d1-context-regression.xml`、`d1-context-final.xml`，哈希在stage-b-progress.json。全部进程已退出，没有新增后台服务或Provider调用。不是正式事务、真实校准或真实旧书接入验收。

历史D1第一份候选代码提交c3093b0：17项来源检查及`.tmp/stage-b/d1-attempt-1/edbacc71913149d4b6265f944da22c78`当时报告`production_bridge=pending`。该记录不再代表当前结果，旧证据保留。候选存储随后加入明确确认的静态引用，标识为`d1-reviewed-state-experiment-2`；上一份试验数据不是兼容承诺，正式格式留D2确定。

旧路径基线原始观察已核对：`.tmp/stage-b/d1-baseline/0e19a5bfefcc4cbd80c3dd5474b40418/observations.json`。独立审查子代理`d1_source_review`已完成且只读；其指出的首次受管写入换行误报已在候选修正。下一步可由根代理继续，不需重复代理调查。

T8.0完成：`tests/fixtures/chapter_fact_offline_v1/manifest.json`及12个用例，`scripts/check_fact_offline_suite.py`可运行。默认不读取正式作品；加`--verify-local-sources`核对三份正式材料及隔离副本。结果`.tmp/stage-b/offline-report-initial.json`：8项确定性符合预期、4项policy_gap、0项回归；`--require-policy-complete`应返回1，防止把样例准备当策略完成。测试3通过，XML和哈希在stage-b-progress.json。真实快照位于`.tmp/stage-b/offline-source-snapshots`，未加入Git。注释均为agent整理，人工复核pending；三份真实材料没有伪造为输入输出配对。

T2.1设计见`docs/development/chapter-fact-contract.md`。纯入口`core/engine/chapter_fact_prepare.py:prepare_confirmed_fact_changes`从上游绑定的authoritative_state计算before/after、名单人数和事件依赖，不读取或写入作品。未知实体/数量/位置/字段基线拒绝，T2.2再补显式声明；source_tier、身份等字段不允许经普通状态更新改变。现有领域校验器与名单reducer继续使用，未解决D1旧书来源优先级。T3.2必须把结果绑定到实际书籍版本、read-set、正文和事实决定后才能提交。

最新事实综合回归session15752已退出（72通过，16.10秒）；最后仅新增测试，准备测试23通过（0.56秒）。XML及哈希在`stage-b-progress.json`。辅助代理在准备层实现开始前因额度不足失败，未留下该层文件，根代理完成实现与验证；不反复重启代理。

本次T1提交覆盖启动器Python入口、instance/app/jobs/projects、保存目录标签、三份相关测试及阶段A文档；实际commit以当前Git log为准。所有新测试使用隔离样本，无Provider调用、正式书籍写入。设计见`docs/development/workbench-instance.md`，浏览器报告见`stage-a-browser-verification.md`。

T2.1本次提交范围：chapter_fact_candidate/contract/prepare、七类payload schema与合成例子、两份新测试及文档。原v1合法载荷形状保持，含重复事件/变化的旧无效记录可能被新校验拒绝，不能静默重写原始模型建议。本轮没有新的网页功能入口；浏览器记录仍只证明T1.2。

T0.2新增入口：`scripts/setup_environment.py`、`scripts/check_environment.py --dev`、`requirements*.lock`；正常开发用`.venv/Scripts/python.exe`。Windows启动器已固定为项目虚拟环境并做本地依赖预检；更新bat后保持CRLF（.gitattributes已有约定）。旧书来源试验已开始，兼容和可写恢复试验仍待执行。

最终版保留两模式同轮交付；恢复范围收敛为Windows同机单目标作品，但必须能恢复后继续写作。真实验收的30%节时为观察目标，失败/放弃不得从统计中删去。

用户已要求按阶段实施整个计划，目标保持active。直接按上述下一条工作推进，不需要再确认是否实施；真实模型费用授权仍未提供。
