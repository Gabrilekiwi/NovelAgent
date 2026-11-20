# T3.2：本地质量检查与发布请求索引

日期：2026-09-09。基于 `3d30773`。状态：内部组件已验证；完整 T3.2 尚未完成，普通 identity 3.0 入口仍关闭。

## 本次完成

`core/engine/reviewed_chapter_quality.py` 的 `prepare_reviewed_chapter_quality` 接受服务端核验后的事实准备和真实上下文，使用已审核七类状态构成 snapshot，而非 Candidate 的空缓存种子。复用 `validate_chapter(enable_llm=False)`、最终正文完整性检查、权威状态 delta 验证、本地审读/叙事规则、独立 review gate 和统一 quality decision。使用上一章经过核验的 review tail，前后重验 read-set 与规则包。报告绑定最终正文、事实准备、状态、上下文、策略和规则包。

现有 legacy Strict 身份与 `quality_policy='strict'` 是不同约束。默认 standard 的本地审读不调用 Provider；strict 策略要求模型校验，当前适配明确保留缺失阻断。所有结果均为 `local_quality_checked`、`publication_authorized=false`、`quality_coverage_complete=false`；`candidate_scene_and_blueprint_evidence` 仍待正式服务接入。即便 `local_quality_passed=true`，也不能直接提交。

`core/engine/chapter_commit_request.py` 固定 `reviewed_chapter_request_v1`。外部请求只有登记/章节/审核 ID、正文与事实的预期修订/哈希及幂等键。归档保存 book 范围的键哈希、完整请求及完整服务端事实准备，不保存原始幂等键。请求和 CAS 必须与准备绑定一致。格式不证明作者身份或质量批准；作者确认仍由原存储适配核验。

请求作为 `reviewed_commit_request` 必要发布 artifact，固定路径 `reviewed/requests/<run_id>.json`。最终记录同时绑定 client_request_sha256、idempotency_scope_sha256、request_evidence_sha256。`read_verified_commit_requests` 先验证完整历史、实际 v2.1 receipt/marker/manifest/正文/状态，再将归档准备与原 read-set、事务上下文、重新计算的章节文档及最终记录逐一核对。历史 read-set 不针对已经推进的下一章重新计算。泛用事务成功不能代替请求绑定；缺失/空请求、错路径、错上下文、错最终绑定均被拒绝。

`workbench/chapter_commit_index.py` 提供 `ChapterCommitIndex`，SQLite 中的 `chapter_publication_index` 仅为已发布请求的索引。每次 `find_published` 都先核验文件历史并重建该书索引，再按原请求查找。同键异请求直接冲突；同一请求已发布时，无需重新读取/准备已成为历史的作者草稿，也不重调模型。索引损坏或缺行可以重建；文件凭证损坏则报错，数据库旧成功行不能兜底。数据库原生作者草稿、登记和整个 workbench DB 的恢复不在这项证明范围内。

重建在同一数据库事务中读取项目登记并替换该书索引。外部调用者可传入自己的事务，索引层不提前 commit；错误数据库或无事务连接被拒绝。作品锁、维护归属和待恢复事务检查由上层服务负责，当前索引层不宣称承担这些职责。查找返回 `None` 不代表允许新提交：还必须核验未完成/已回滚请求和当前准备。

## 已运行验证

- `.tmp/stage-c-commit-index-regression.xml`：74 passed，含 23 项新请求/索引测试、29 项正式状态测试、22 项身份升级测试。SHA256 `dda40e985a866fa6397b2d323b4e4585724d2bfbc664c48a4df9414cdb26a2a7`。
- `.tmp/stage-c-reviewed-quality-gate-regression.xml`：67 passed，含 18 项新质量检查及原准备/quality decision/review gate/final integrity 回归。SHA256 `05305e527dd0a78570757379c1b020735cbe7da88ae328ea5c956d743da775b9`。
- `.tmp/stage-c-quality-request-bridge.xml`：1 passed、23 deselected，新增真实 Candidate→持久草稿/作者确认→正式格式上下文→质量/请求准备联通；未写入正式章。

新增测试合计 42 项。索引场景包括：真实文件事务完成而数据库尚未写索引、改稿/游标推进后的重复查找、同键换六种请求内容、索引行损坏/删除、冷进程重建、损坏磁盘凭证、真实泛用 receipt 仍缺少必要请求证据、数据库外部事务中断、跨登记/目录和错误连接、提交标记前回滚/标记后完成原事务。故障发生后、reconcile 前不会查得成功；恢复不重新生成。

早期 `.tmp/stage-c-commit-index-initial.xml` 为 20 passed / 1 failed：测试误用了不存在的 `after_apply` 事件，未实际触发中断。已改为真实 `after_apply_target` 并通过上述回归，失败文件保留。质量首轮发现旧 identity 1.0 原始 JSON 无 authority 字段，改为复用现有 identity validator 的 authority 投影；最终通过证据以上述 XML 为准。

隔离目录为 `.tmp/cp3` 及相关既有合成夹具。发布 fixture 使用真实 v2.1 prepare/commit/reconcile，但它不是产品提交服务，没有绕过现有持久化 guard。当前联通仍是内部正式状态 loader + 旧身份的合成夹具，不是已升级 v3 作品的 Candidate/commit 验收。没有 Provider 调用、真实作品写入、UI 改动或全套 pytest/smoke。本轮会话均已结束。

有界子代理完成质量实现及请求/索引只读审查，未发现可复现的新增绕过；这不代替后续完整服务验收。

## 下一步

1. 从服务端已验证的 Candidate intent/记录及实际上下文补齐 scene/blueprint 校验，保留作者改稿的最终字节语义，不把合成声明伪称为生成证据。组合完整质量门并归档原始证据。
2. 将作品锁、DB 写事务、读前恢复、原请求意图识别及锁内重验接到真实正式章节事务。固定请求在 pending/回滚阶段的幂等语义；索引查找本身不完成双击单一逻辑提交。
3. 联动内部 v3 上下文、Candidate/事实准备、read-set/事务 writer/recovery/job 契约，先完成实际升级后的逐章提交和冷读，再启用普通入口；不可只提高全局 writer 契约而放行旧 writer。
4. 接 API/UI，完成 T3.2 全套 pytest/smoke、实际浏览器与 EA 相关回归。真实作品升级仍等待维护/未知 Provider 与事务处理、T7.1 备份、T2.2 接入预检；T7.2 按最终格式复验可写恢复。
