# reviewed_legacy_state_v1 实施记录

2026-09-09，T3.2 中间检查点。codec、凭证历史读取和内部上下文桥接已实现；普通入口尚未启用，ProjectIdentity schema 仍为 1/2，writer contract 仍为 1。这里的隔离事务测试不代表 identity 3.0 升级或产品提交服务已经完成。

## 存储与来源

正式候选格式位于 `追踪/已审核事实.md`，使用现有 semantic managed block；`scope=context`、`parser_version=reviewed_legacy_state_v1`、唯一 owned field 为 `reviewed_state`。`core/story_project/reviewed_state_document.py` 只返回文件字节，不发布、不赋予作者确认或写入权限；输入确认必须由服务端取得。

受管值的字段固定为：

| 字段 | 约束 |
|---|---|
| format / revision | 固定格式名；正整数修订，每次基线后转换加 1 |
| initial_baseline | 保存原始七类状态、确认对象、人工来源版本、静态引用；后续转换不可修改 |
| source_revision | `追踪`、`设定`下 Markdown 人工部分的路径成员和哈希；排除本状态文件，移除受管块，仅规范化末尾 CR/LF |
| current_state | 保留完整七类集合和规范元数据；不得以缺字段归一化为空集合；人物位置镜像须一致 |
| committed_through | 明确的已提交章号，与受管 projection 的 chapter 一致 |
| source_decisions | 持久化的人工来源冲突决定，绑定旧文档、修订、当前状态、前后来源和差异 |
| static_references | 已确认原文片段及整文件哈希；引用来源后改不能自动接受 |
| last_transition | 当前章节的事实、正文哈希、before/after、审核修订和幂等范围；或本次来源冲突决定 |
| head | kind、run ID、receipt ID/相对路径、request 哈希及前一文档哈希/凭证路径 |

基线确认绑定 book_id、截至章号、完整状态、人工来源、静态引用和升级前 identity 文件哈希。该对象不是签名，也不独立证明作者意图。正式升级服务仍须核验真实确认、Strict 资格和备份预检。

新正文先按 writer 的 `rstrip() + "\n"` 规范化；codec 从事实重算状态，核对已准备结果，并验证转换后再返回字节。人工来源变化只支持已验证的 D1 决议 `keep_reviewed_current`，要求具体理由和作者来源；静态引用已改变时不能用此决定绕过重新审核。

路径检查复用 SafePathResolver 的物理目录 guard；读取前后核验文件身份，不跟随 symlink/reparse，不允许 hardlink 来源。Markdown 扩展名大小写均纳入来源集合。包含真实书籍的任意外部路径不是 codec 参数的默认读取目标。

## 凭证、历史与冷读

每次转换将当前状态作为 apply target，并把相同字节作为不可变状态 artifact 发布。章节转换另有正式正文 apply target 和正文 archive。约定的 runtime 相对目录为 `reviewed/receipts`、`reviewed/states`、`reviewed/chapters`；测试事务目录为 `reviewed/p`。最终产品执行器须沿用绑定约定，不能用普通文件替换代替事务。

head 绑定未来凭证的位置，凭证绑定当前文件和归档的哈希；不在状态文件内嵌其自身凭证的最终哈希，因此没有循环哈希。最终记录使用 `record_type=reviewed_legacy_commit`，绑定 kind、book、run、revision、request；source_revision_after 同时绑定状态字节和章号。

`core/story_project/reviewed_state_store.py` 使用真实 `verify_publication_receipt` 验证 v2.1 manifest、marker、归档和最终记录。它还检查当前文件必须等于所有已保留凭证的唯一末端，状态归档与凭证一一覆盖，按父文档哈希追溯至由调用方提供的基线锚点，再用每章事实和归档正文重算转换。重复修订、断链、分叉、旧文件回退、丢失凭证、伪造当前数值和当前正文后改均拒绝。正式正文必须唯一对应其章号，实际章节游标必须与状态游标一致。

这一验证针对本地完整性及意外损坏；不是防止攻击者整体重写作品和全部历史的外部签名系统。旧凭证验证本身不核对可变 apply target 的当前文件，新层补上了当前状态及正式正文检查。

`load_runtime_view` 把上述核验结果交给已有 read-set-bound context view。当前只能由内部显式注入：基线锚点和可信 root_map 后续必须来自 identity 3.0/升级凭证，不能来自客户端。当前没有默认文件发现、隐式 D1 转换或真实作品升级。

## 验证范围与后续

`tests/test_reviewed_state_document.py` 使用唯一 `.tmp/rs3` 目录。普通 codec 样本为 synthetic shadow identity；实际工作台桥接样本使用既有 synthetic Strict 资格，仍是 identity 1，显式注入新 reader，绝不冒充 v3 产品支持。

隔离发布 helper 使用真实 PersistenceV2Transaction、完整磁盘 read-set、精确声明写入、归档、最终记录及凭证，没有 patch 持久化 guard。初次 helper 缺少 read-set 被真实 guard 拒绝；修正了夹具，没有放宽保护。标记前故障回滚，标记后故障恢复原始字节；读取器不会自己恢复或重新生成。

实际候选/草稿/审核/服务端准备桥接已验证：第三章使用凭证核验状态，测试事务提交后第四章读取警戒值 4；生成仅一次。新进程冷读独立证明七类状态无缓存可恢复。完整的故障注入、API/UI、质量检查和幂等提交验收仍在 T3.2 后续工作中。

证据：`.tmp/stage-c-formal-state-final.xml` 综合 116 项通过；边界修订后的最终 XML 和哈希见 `stage-c-progress.json`。之前通过的 23/44 项，以及首次缺 read-set 的失败 XML 保留，不重复计为新覆盖。

下一步落实 identity 3.0 的来源/基线绑定和升级事务，再接产品作品锁、待恢复事务门、正式质量批准、文件幂等记录/可重建 DB 索引、预览/提交 API/UI。所有服务必须在规定锁与 CAS 边界下复查；这个只读 reader 不替代跨进程所有权或事务恢复调度。当前每次冷读核验完整保留历史，性能优化必须保留这些证据约束。最终格式仍需实际 B0 拒写复测和 T7.2 可写恢复；不能删旧历史或放宽旧物理 guard。
