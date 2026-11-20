# T3.2：审核状态身份升级

2026-09-09，内部实现检查点。identity 3.0 codec、受约束升级事务和升级凭证冷读已通过隔离验证；普通项目 schema 仍只接受 1/2，`CURRENT_WRITER_CONTRACT` 仍为 1。没有开放升级 API 或改动真实作品。

## 绑定与事务

独立 `schemas/reviewed_project_identity.schema.json` 和 `core/story_project/reviewed_identity.py` 定义新元数据。它复用现有 identity/activation/authority 校验，保留 book_id、created_at、Strict activation，root_hint 为 `.`；authority 保持 legacy epoch 0、无 event head/activation receipt，最低 writer 契约为 2。

新增 reviewed_state 字段明确绑定格式、来源相对路径、基线文档哈希，以及升级 run/receipt 位置、旧身份原始字节哈希、基线确认和原校准报告哈希。只有绑定真实旧身份字节的 baseline 文档可用于升级；不能直接发现或采用已有 D1 试验文件。

`core/engine/reviewed_upgrade.py` 从真实旧版 Strict 候选上下文准备升级。它要求原始校准报告与已激活身份的哈希、作品、parser/schema/profile 一致，原正文章号连续且不重复，基线确认仍匹配当前来源。准备阶段只返回数据，`publication_authorized=false`。创建事务前再次读取完整上下文并重算准备结果，不以重新计算客户端哈希作为有效确认。

内部 staging 函数使用既有 PersistenceV2Transaction；原始 identity 字节完整保留为 artifact。只有两个 apply target：新的审核状态和新 identity。其余发布 evidence 包括状态 archive、identity before/after、校准报告、完整 preparation 及最终记录。章节正文与旧不可变历史不在升级写入集合中。已存在的审核状态拒绝覆盖。

新增 `legacy_reviewed_format_upgrade_v1` 明确区分格式升级与 EA head 推进。底层 guard 验证精确 target 集合、before/after、来源、完整 preparation 和 actual transaction 的 run/context/generation/receipt 绑定，才允许 legacy epoch/head 保持原值。普通无类型的同 epoch/head identity 写入继续拒绝；EA 原有推进规则不变。

升级 declaration 只在 `during_apply/pre_marker` 允许读取哈希相等、来源/旧身份绑定一致的新 identity after-image。正常读取与执行不因此接受 v3。准备与 pre_apply 仍要求旧身份；任何来源、身份、目录或目标文件变化继续受原 read-set 和物理 guard 约束。

## 冷读与恢复

`load_verified_reviewed_identity` 为显式诊断/升级读取入口。它验证 identity→升级 receipt→before/after/校准/基线链；`reviewed_state_store` 进一步核对当前 identity after-image 和完整 preparation/final request，并从已验证 manifest 取得原 read-set。普通当前来源冲突检查仍属于 `load_verified_current`，元数据诊断不授予执行权限。

共享 `verify_upgrade_preparation_evidence` 在 typed prepare 和冷读中使用同一套精确绑定。独立审查曾发现仅检查 preparation target 存在、未解析内容的缺口，已修复；空对象、错误归档路径、错配 context 和最终 upgrade request 均在真实底层 prepare 被拒绝。限定复查未发现新增可执行缺陷，没有声明独立审查代替运行测试。

实际故障注入覆盖写完状态但未写 identity、写完两者但无 marker、marker 已落盘三处。前两处恢复原 identity 并移除未提交状态；后一处完成原始升级事务。新进程可核验升级后的 identity 和七类基线，不调用模型、不重新生成。

## 验证证据与剩余门槛

22 项升级测试包括旧 identity 1 和 legacy identity 2、保留 Strict/创建信息/旧正文、来源后改、重新哈希的伪准备、typed bundle 拒绝、正常入口拒绝、真实冷读调用共享 preparation verifier，以及上述三处中断。

- `.tmp/stage-c-upgrade-reviewed-regression.xml`：123 项通过，7 项子测试通过，含 EA v2.1、read-set、D2 入口和 schema 回归。
- `.tmp/stage-c-upgrade-formal-bridge-final.xml`：最后 72 项通过，含升级、正式状态与工作台准备桥接。
- `.tmp/stage-c-upgrade-binding-regression.xml`：前一阶段 52 项及 4 项子测试通过，不与最终结果累加。

最初事务缺 runtime 目录被 RootRegistry 拒绝，已用安全路径检查初始化；后续 pre-marker 仍调用旧 schema 被拒绝并回滚，已补传实际 phase 到内部升级读取。初期失败 XML 保留，最终结果以机器进度中的哈希为准。相关测试进程均结束；未运行全套 pytest/smoke 或浏览器。

这不是产品升级入口。调用方仍须落实维护/作品所有权、运行进程退出、未结算 Provider 和未完成事务检查、T7.1 可恢复备份、T2.2 实体/人工基线预检；本文内部 staging 函数不把这些尚未实现的门槛伪装成完成。当前只操作 `.tmp/iu3` 等合成样本；真实 Strict 校准不能由合成证据代替。

下一步是正式章节 commit：在规定作品锁与 DB 锁内重验草稿/事实/来源，核验最终正文质量，保存幂等请求及可重建 DB 索引，接预览/提交入口和恢复。新 identity 的普通读取、候选生成、正式写入和恢复必须一起集成验证后才提升 schema/writer/job 契约。尤其不能仅放开普通 v3 gate，让旧 legacy writer 不经新提交服务写入新格式作品。最终还需 B0 拒写复测、完整 T3.2 pytest/smoke/browser 和 T7.2 可写恢复。
