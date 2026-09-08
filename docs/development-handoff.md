# NovelAgent 开发交接

更新时间：2026-09-08。

## 当前任务

正在按最终执行版推进阶段A。T0.1基线已保存，T0.2环境已实现并通过项目环境及中文/空格路径全新安装验证；下一项是T1.1单实例和执行子进程所有权。完整开发目标仍未完成。

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
- T1数据目录锁还需覆盖父进程强制结束时执行子进程的存活情况；已取得服务锁不等于旧执行者已停止。

## 已完成验证

2026-09-07审查基线：1905 tests、561 subtests通过；8跳过；本地smoke通过。日志在`.tmp/project-review-20260907`。9月8日计划制定与复审只做定向只读检查及文档更新，没有重跑全套测试，也没有模型调用、产品代码实现或真实书籍写入。最新恢复/子进程风险是代码审查结论，尚未执行新的故障注入。

9月8日阶段A实施：35项锁定依赖检查和pip check通过；项目环境相关测试最初101通过、1跳过、2子测试通过，1项启动器混合换行失败，已修复。修复后启动器2项通过；全新中文/空格路径环境的工作台app测试18项通过。项目环境smoke通过，21/21预检、隔离提交与报告通过，无Provider调用。详情和原始证据见`docs/development/stage-a-progress.json`。未执行远端CI，未声称全套测试本轮重新通过。

## 工作包状态

| 编号 | 状态 | 下一步 |
|---|---|---|
| T0.1 | 已完成 | B0代码提交、48文件原始归档和哈希清单已核验 |
| T0.2 | 已完成 | 35项依赖锁、项目环境、全新安装、启动器、文档与CI安装入口已完成本地验证；远端CI未运行 |
| T1.1 | 下一实现任务、待开始 | 应用初始化前取得数据目录所有权，覆盖执行子进程；先检验父死子活路径 |
| T1.2 | 待开始 | 目录输入结构化4xx及中文提示；随T1验证真实浏览器 |
| T2–T7 | 范围与子任务已确定、待实现 | 按最终版阶段A–F推进；D1旧书来源、D2格式兼容、D3可写恢复先做有限试验 |
| T8.0 | 离线准备待开始 | 使用已有正文、响应和合成样本，无新付费调用 |
| T8.1、T8.2 | 独立阶段、未授权真实调用 | 早期逐章配对与最终两模式连续验收分别限定预算；缺授权可继续独立工程 |

当前没有本任务创建的持续运行服务或测试进程。安装和测试工具session均已取得退出结果；阶段A辅助代理因额度不足终止，根代理完成环境工作。系统中原有pythonw进程15004未操作，不应把它视为本任务创建的服务。

## 下一条工作

下一步T1.1：先读`run_workbench.py`、`workbench/app.py:create_app`初始化/生命周期及`workbench/jobs.py:JobSupervisor`；当前create_app在进入lifespan之前就初始化数据库和执行planner/amendment恢复，锁必须早于这些动作。执行由Popen子进程完成，不能只锁HTTP父进程。现有测试多用create_app(start_worker=False)建立隔离服务，设计需覆盖这些生命周期，不以测试选项作为正常启动绕过锁的入口。

T0.2新增入口：`scripts/setup_environment.py`、`scripts/check_environment.py --dev`、`requirements*.lock`；正常开发用`.venv/Scripts/python.exe`。Windows启动器已固定为项目虚拟环境并做本地依赖预检；更新bat后保持CRLF（.gitattributes已有约定）。旧书来源、兼容和恢复试验尚未执行。

最终版保留两模式同轮交付；恢复范围收敛为Windows同机单目标作品，但必须能恢复后继续写作。真实验收的30%节时为观察目标，失败/放弃不得从统计中删去。

用户已要求按阶段实施整个计划，目标保持active。直接从上述T1.1推进，不需要再确认是否实施；真实模型费用授权仍未提供。
