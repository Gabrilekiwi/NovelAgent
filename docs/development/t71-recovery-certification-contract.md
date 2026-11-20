# T7.1 可恢复认证边界与下一包恢复绑定契约

2026-10-19 当前状态核对：原登记与恢复来源的升级、旧归档关系及故障回退已有内部证据；服务启动以来源码/依赖锁约束已实现，但原生浏览器复验受连接限制。当前认证真实欠项和源码依据见 `t71-certification-current-audit.md`，启动版本证据见 `t71-source-version-session.md`。本文后续设计表中的“尚无交接/隔离”等是历史语境，不代表当前代码；完整认证及公开作者基线/升级仍未完成。

日期：2026-09-12。性质：基于当前源码的实施建议与验收契约，**不是已实现功能或通过验收的声明**。本次只新增本文档，没有修改产品代码、运行测试或访问真实作品。

后续实施更新：受管历史发现、新旧事务、reviewed/legacy当前状态、完整DB、引用、辅助状态、预算/调用、候选/事实、任务的离线核验已接入统一入口；15项组合/legacy和12项恢复位置计划测试全部通过，详见 `t71-archive-verification-plan.md`。本契约的只读位置计划已有实现，plan_ready不授予写资格；认证签发及T7.2实际恢复/隔离/交接/续写仍未完成。下表保留设计时语境，不重复开启已完成的离线适配工作。

2026-09-14 更新：启用前目标访问/调度隔离 13 项与实际复制 17 项已验收，见 `t72-restore-scope.md`、`t72-restore-materialization.md`。原目录不可用的整库与各资料树复制、两种真实进程中断后的拒绝启动已验证。仍缺新物理运行绑定、目标 SQL 重定位、同机写资格交接和最终下一章；复制不代表可恢复认证，下表关于“尚无隔离”的文字仅代表设计时历史。

同日后续：实际复制产物的可变 registry 新物理绑定与历史只读映射新增 12 项通过；正常 reviewed 冷读、旧 guard 拒绝、新进程重复与强退续接已验，见 `t72-restore-roots.md`。数据库仍原字节且启动被阻止。目标 SQL、可变引用、完整工作台恢复入口、同机资格交接、新执行目录、最终下一章及恢复后备份支持仍须完成，不能将根绑定凭据作为写资格。

依据为[最终开发计划](D:/Pycharm/PythonProject/NovelAgent/docs/development-plan-final-2026-09-08.md)、[新旧事务统一历史检查](D:/Pycharm/PythonProject/NovelAgent/docs/development/t71-mixed-history.md)及下列源码。本文不取代唯一主计划，不把 T7.2 的可写恢复、同机交接或续写验收提前标记完成。

## 1. 有限支持范围

T7.1 的目标是 Windows 同机、单目标作品的完整可恢复备份，覆盖当前受支持的 legacy Strict 作品及其升级为 reviewed 3.0 后保留的新旧历史。整份 SQLite 随包保存，但不因此成为完整工作台备份。

纳入认证的范围应逐项显式记录：

- 登记目标的 `book_id`、`project_id`、格式与写入契约；完整作品文件，包括升级前旧正文、不可变历史和人工内容。
- 作品内 runtime、模型调用及响应、提交凭证、状态、任务和预算证据；已登记目标任务位于服务器配置任务目录中的外部产物。
- 从上述受管元数据声明出的必要根和文件。外部根不能凭 JSON 中的路径自动获得访问权；目标确有额外必要根时，必须提供明确的归属、清单及受控停写方案，或继续阻断认证。不得删引用、略过文件或缩小目标作品范围以通过检查。
- 完整 SQLite、作者草稿、批次及任务位置、非秘密运行配置、版本和文件哈希清单；凭据及 `.env` 排除，恢复时重新配置。

支持入口由当前产品入口和已有竞争测试限定：工作台写请求与 worker；正常 legacy/reviewed 生成、正式提交与恢复；已修复的 runtime 初始化、身份激活、草稿恢复、审阅输出/索引、旧运行目录迁移、作品内 delivery/autonomy 公共写入口，以及向作品内写文件的投递适配器。应把这些入口对应的锁与测试保存为可检查清单。

这不要求防止任意历史程序、任意私有函数、任意脚本直接改文件或其他机器上的副本写入。未知后端/布局不能默认为上述支持范围；它若是目标作品当前或保留历史所必需，则是具体阻断项，而不是可以丢弃的数据。本文未读取真实作品，因此不判断真实目标是否存在额外必要根。

## 2. 当前已有证据与具体欠项

| 条件 | 当前已实现的证据 | 仍欠工作 |
|---|---|---|
| 停写和一致快照 | `locked_backup_source` 协调维护许可、作品状态、机器所有权、Provider、SQLite、EA、迁移和局部持久化锁；源清单在释放前复查；`owned_database_snapshot` / `capture_database` 使用自有事务及 SQLite backup | 收束受支持入口/布局清单，不能把已经证明的范围继续称为“所有外部入口未知” |
| 范围和引用完整性 | `inspect_backup_scope` 收集目标整树和目标 job 外部目录，核对 DB、事件、最终运行记录及嵌套 JSON/逻辑根引用；未知、缺失或被排除的必要项阻断 | 对目标实际涉及的额外必要根建立受控映射；不要求支持任意未使用布局 |
| 新旧历史 | `verify_persistence_history` 联合 v1/v2 全目标逆向图；`read_v2_transactions` 核对清单、暂存/前像、marker、receipt、终态登记；reviewed 冷读继续核对类型化状态、升级与正文证据 | 独立复审新发现受管根扫描漏孤立 registry、误认作者普通 `journals`/`transactions` 目录；主代理正在修复，本文不将其视为已验收。修复后按受管命名空间完成验收，不再重新列“多章 legacy 链未实现” |
| 未决状态 | 目标任务、候选/事实抽取执行、模型调用/响应、预算/时间和已识别 delivery/autonomy 控制记录均有只读检查 | 辅助检查的 `complete_history_certified=false` 不能被忽略，也不应直接扩大成所有后端重做；按目标实际涉及的辅助内容说明其必要证据与覆盖范围 |
| 包完整性 | 预览摘要绑定来源；ZIP、DB、目录、成员与清单逐一对应；最后发布 completion；`verify_archive` 只读核验 | 当前所有产品包仍为 `diagnostic` / `recoverable=false`；没有认证签发器 |
| 版本 | `fingerprint_software` 保存磁盘代码和依赖锁哈希，归档前后复核；配置限定非秘密字段 | 区分版本证据与运行进程内存证明，见下一节 |
| 整库中的其他项目 | scope 与 DB 描述声明恢复时默认禁用 | 尚无访问/调度强制执行。`ProjectRegistry.get/list_projects` 无恢复范围过滤；`JobStore.claim_next` 会领取任意 queued 项目。不能把声明字段当作隔离已生效 |
| 恢复新绑定与资格 | 包保存源根、原 registry、身份及 reviewed 机器所有权证据；D3 有有限实验 | 当前历史验证依赖源绝对路径；产品 `BookOwnership` 只有 enroll/require，没有 restoring、交接和故障续接。D3 单事务实验不等于当前混合历史产品恢复 |

相关实现：[backup_source.py](D:/Pycharm/PythonProject/NovelAgent/workbench/backup_source.py)、[backup_scope.py](D:/Pycharm/PythonProject/NovelAgent/workbench/backup_scope.py)、[backup_archive.py](D:/Pycharm/PythonProject/NovelAgent/workbench/backup_archive.py)、[backup_v2_history.py](D:/Pycharm/PythonProject/NovelAgent/workbench/backup_v2_history.py)、[book_ownership.py](D:/Pycharm/PythonProject/NovelAgent/workbench/book_ownership.py)。历史文档中的“下一步”是当时状态，应结合后续组件和当前代码判断，不累加已经关闭的欠项。

## 3. 版本证据的硬门槛与有限建议

主计划要求非秘密配置、版本与文件哈希清单，以及匹配数据格式的恢复/回退。它没有要求证明任意动态 Python 进程的所有内存代码等于磁盘文件。

因此，`loaded_modules_match_proven=false` **不宜单独成为可恢复数据认证的永久硬门槛**。当前字段准确表达已知限制，应保留；不应改成无证据的 true。`loaded_code_version_proof` 目前位于 `BackupArchiveService._preview` 的 pending 列表，建议改为以下可执行版本条件，而不是原样继承为无限工程任务：

1. 归档记录的协议、作品格式、DB schema、写入契约和源文件指纹完整且稳定；未知格式明确拒绝。
2. 实际离线验证器记录自己的版本/实现指纹、支持协议及验证结果；不能把归档宣称的源版本当作验证器的版本。
3. 如果产品要保证正在运行的服务使用同一发布版本，可在受控启动时记录实现指纹，备份前发现磁盘变化则要求重启并重新预览。这是有限运行约束，不是已经实现的内存一致性证明。

长时间运行的服务可能已加载旧模块，这是合理风险；启动记录和受支持启动方式可以减少风险。证明任意 monkeypatch、动态 import、外部注入或历史解释器的所有行为不属于本轮范围。最终稳定版的依赖锁、干净 Windows 安装与匹配版本回退仍由 T7.3 验收。

可恢复认证不能仅凭版本字段通过：结构、范围、历史与来源核验仍是硬门槛。哈希提供内容绑定与损坏检测，不是对恶意整体重写历史的数字签名。

## 4. 下一包：离线验证归档并生成确定性恢复绑定计划

建议本包只交付一个离线验证/计划组件及隔离验收。它不启动工作台、不注册作品、不创建可写恢复目录、不执行旧准备计划、不修改机器所有权，也不调用 Provider。必要时可以使用独立临时目录读取已验证的 ZIP 成员或打开离线 DB；临时材料不构成启用的恢复副本。

### 输入

| 输入 | 数据与约束 |
|---|---|
| 已完成归档包 | `archive.zip`、外部 `manifest.json`、`completion.json`；必须先由 `verify_archive(package)` 通过当前格式检查，缺 completion 拒绝。当前 diagnostic 包可作为验证输入，但其用途和原 `recoverable=false` 保持不变 |
| 目标绑定 | 显式 `book_id` / `project_id`，必须等于 manifest、scope、SQLite 登记及作品身份；不自动选第一条登记 |
| 兼容性规则 | 验证器明确列出的包协议、作品格式、DB schema 和受支持根布局；源程序哈希不同不等于必然不兼容，但未知数据契约必须阻断 |
| 拟恢复位置 | 单目标作品根、恢复 DB、目标 job 产物根等明确位置；读取现有父目录身份并检测同源/嵌套/重叠、Windows 别名及路径预算，不创建或启用目标。归档内的旧路径只是来源证据，不能触发对旧目录的读取 |

必须在源目录不存在或访问被拒绝的条件下完成验证。不能使用 `inspect_backup_scope` 的现有 live registry 参数直接打开源 DB；也不能构造会 initialize/recover 的工作台数据库、项目或运行时存储对象。

### 核验数据契约

建议增加只读的“归档成员视图”，把逻辑来源定位转换为已核验包成员。接口名称尚未确定，以下是行为约定：

- 根表以 scope 的 `story_project`、`jobs/<id>` 等来源键和 manifest 的 `archive_path` 为权威；每根绑定 `source_root`、`inventory_sha256`、成员大小/哈希、必要目录与排除项。
- v1 绝对目标先按已归档来源根解析成成员键，再验证历史；不得在新目录覆写旧 manifest 中的绝对路径，也不得把不存在的旧路径交给原 reconcile。
- v2.1 路径同时核对 `registry_id`、root ID/UUID、原 registry 摘要、PathRef 与成员归属；旧 physical identity 作为历史保留。源身份、恢复目标当前身份、未来激活身份是不同绑定，不能互相冒充。
- 混合历史目标图与 reviewed 业务证据都需要离线重验。复用验证规则应通过显式读取/映射接口；不得全局 monkeypatch 路径解析或暂时放宽普通 `SafePathResolver`/PathGuard。
- SQLite 是完整、无外部 WAL 依赖的离线快照：只读检查 integrity、foreign key、schema 与全库逻辑摘要。直接只读查询目标草稿、任务、批次、预算相关索引及其文件引用；不因打开 DB 运行 schema 初始化或任务恢复。
- 未知根、必要文件缺失、作用域歧义、非终态、预算/调用不确定、损坏或超出明确计算上限均输出具体 blocker；不返回截断成功。

### 输出

建议新增版本化结果，例如 `target_restore_plan_v1`；这是**拟议 schema**，当前代码尚不存在。应包含以下字段组，而不是仅一个 `recoverable` 布尔值：

| 字段组 | 必须绑定的信息 |
|---|---|
| `archive_binding` | archive ID、ZIP 哈希/大小、manifest/preview/scope/completion 摘要、源 DB 逻辑摘要；原包用途照录 |
| `verifier` | 验证器协议、版本/实现指纹、接受的数据格式与布局；不混同源软件版本 |
| `target` | book/project ID、身份格式和摘要、已提交位置、混合历史摘要及 reviewed 头证据；源软件/配置证据 |
| `root_plan` | 每个必要根的来源键、源路径、包内位置、拟恢复位置；旧 registry ID/UUID/摘要、拟保留的逻辑身份和需新建的物理控制绑定 |
| `database_plan` | 原整库摘要与目标行/草稿/批次/任务等保留证据；需要重绑定的目标登记及已识别产物引用；其他项目 ID 全集的默认禁用计划 |
| `execution_policy` | `start_worker=false`、`auto_import=false`、`provider_calls_allowed=false`、`write_activation_allowed=false`；旧准备/任务不得自动重放，保留诊断证据，后续重新准备 |
| `ownership_requirement` | 归档中的原所有权证据或 `legacy_not_enrolled`；启用前必须检查当前机器记录并完成交接。不能简单把归档 generation 加一视为当前有效新代际 |
| `checks` / `blockers` | 每项验证的范围、结果和证据摘要；区分包完整性、离线数据验证、计划就绪、写入授权 |
| `plan_sha256` | 对稳定排序的完整计划语义计算摘要；相同归档、目标参数及观察到的父目录身份得到相同计划。时间戳等非确定性审计信息置于摘要外，不生成随机激活资格 |

成功结果最多表示 `offline_data_verified=true`、`plan_ready=true`，仍必须 `restore_authorized=false`。未知前提不伪装为成功。该结果不改原 diagnostic 包和 completion，也不提前签发正式可恢复认证；后续认证签发方式需要明确版本化规则。

当前原机器账本可能在备份后继续推进；离线计划不要求访问账本，更不能根据旧归档宣布原实例已停用。实际 generation CAS、停用原资格及新资格激活属于 T7.2。

### 验收条件

1. 用现有隔离夹具构造多笔重叠 legacy 事务→真实格式升级→reviewed 新章，包含作者草稿、批次位置、已结算预算/响应及目标外部 job 产物；仅从归档验证全部证据与计划。另保留升级前 legacy 归档正向用例。
2. 禁止读取源目录后仍成功；对源目录、原 DB、包内容、机器所有权记录做前后比对，原字节不变。禁止数据库初始化、自动恢复、服务启动和 Provider 构造/调用。
3. 逐项破坏旧/new marker、前像、receipt、终态登记、必要根/文件、DB 或预算/响应，拒绝计划就绪且保留具体原因。孤立受管控制记录不能遗漏，普通作者同名目录不能误识别为事务根。
4. 不改旧 manifest/hash/PathGuard，不使用旧 prepared plan。原始历史字节保留，混合目标链和 reviewed 类型化冷读都通过；不能仅以 ZIP 哈希一致代替内容历史合法性。
5. SQLite 中存在另一作品及 queued job 时，输出完整其他项目禁用集合和零自动执行政策；离线组件不得读取这些项目的外部文件。**这只是计划验收，其他项目在恢复工作台中的强制隔离仍须 T7.2 实测。**
6. 相同输入两次得到相同 `plan_sha256`；更换归档成员、目标位置、必要父目录物理身份或验证器契约后摘要变化或明确拒绝。目标同源/嵌套、目录替换、Windows 中文/空格路径及超出实际路径预算有明确结果。
7. 诊断包即使所有离线检查通过，原用途不被静默升级；有 scope blocker 的故障归档仍可做完整性核验，但不得得到 `plan_ready=true`。本包完成不等于 T7.1/T7.2 整体完成。

## 5. 可复用接口与不能直接套用的部分

| 现有接口 | 复用方式与限制 |
|---|---|
| `workbench.backup_archive.verify_archive` | 复用完成凭据、ZIP、成员和 scope 清单一致性检查；当前仅接受 diagnostic v1，不擅自修改原包标记 |
| `workbench.backup_history.verify_persistence_history`、`backup_v2_history.read_v2_transactions` | 复用 v1/v2 证据与全目标链规则；当前直接依赖本地源根与绝对路径，需要显式归档读取适配，不是把新目录传进去即可 |
| `load_verified_reviewed_identity`、`verify_reviewed_history` | 复用升级与类型化状态/正文证明；现有 live root / registry 物理验证不能被跳过，应分离历史内容核验与新物理绑定验收 |
| `validate_registry_manifest_binding` | 复用 registry ID 与逻辑 UUID 绑定规则；已完成历史允许显式 revision 演进，不意味着可以执行旧 guard 或恢复未完成事务 |
| `backup_references`、`backup_auxiliary`、模型用量/预算验证规则 | 从已核验成员和离线 DB 重新核对必要引用、未决状态；不调用可能修复 current/latest 的运行时状态入口 |
| `backup_database._logical_digest` | 提取/复用只读逻辑摘要规则；offline DB 先确认自包含，不对活跃源库使用 immutable 打开方式 |
| `scripts/experiments/d3_restore_candidate.py` | 复用“保留旧字节、新执行目录、restoring 先于 activation、激活前复核”的设计与故障场景；`verify_v1_history` 和 `rebind_v2_completed_baseline` 仅接受单事务，不作为产品接口直接调用 |
| `BookOwnership` | 复用独立于工作台 DB 的机器锁、绑定与提交前 require；本包只记录激活前提，不调用 enroll 或改写 ownership |

## 6. T7.1 与 T7.2 的依赖边界

下一包将“这些完整归档字节如何在新位置解释”变成可验收契约，解决当前源路径依赖；它不能独自授予 `recoverable=true`。T7.1 后续还需收束支持清单和剩余确定缺陷，并使认证签发器严格依赖一致快照、完整范围、离线验证及支持的恢复协议，不能接受手工布尔开关。

T7.2 保留以下完整职责：安全解包/新物理根建立；对保留的旧历史建立有来源证明的读取映射，重建受控可变 registry；整库默认拒绝访问/调度其他项目，并使隔离在工作台初始化、恢复和 worker 启动之前生效；目标任务/准备计划失效与重新准备；机器账本进入 durable restoring、停止原注册资格、代际交接、故障后显式继续/回退；激活前复核、重启后零自动调用和确定性下一章。

升级前 legacy 备份允许记录 `legacy_not_enrolled`，无需为了备份擅自升级/登记真实作品。其恢复写资格如何与原 legacy 实例交接仍是 T7.2 的明确工作，不能把 reviewed 当前的 require 覆盖误称为所有旧入口已交接。

T7.2 的最终场景必须使用最终新流程的数据，核对正文、状态、草稿、批次、预算与凭证并实测续写；D3 和本离线计划组件都不能代替它。

2026-09-14进度补充：T7.2已实际完成安全复制、根绑定及整库限定SQL重绑定/只读工作台，累计51个新增用例；实际恢复副本Chrome读取/跨作品拒绝/刷新及三个数据库强退位置通过。见 `t72-restore-database.md`。这补上启用前实际应用证据，未完成机器资格交接、激活后范围限制、旧计划失效、新执行目录、下一章或最终认证；原diagnostic包用途不变。
