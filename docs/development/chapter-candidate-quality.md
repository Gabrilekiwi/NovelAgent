# T3.2：候选最终稿的完整标准质量检查

日期：2026-09-09。基于 `bbedefc`；完整提交服务尚未完成，普通 v3 入口仍未启用。

保存状态：本轮 `git add` 被自动审批拒绝，原因为审批服务额度耗尽。HEAD仍为 `bbedefc`，本篇及相关代码/测试已落盘但未提交，未尝试绕过；额度恢复后保存本地检查点。

## 已完成的接入

`ChapterCommitPreparationService.prepare_quality` 只接受现有登记/章节/审核 ID、预期修订/哈希、幂等键和固定的 standard/strict 策略。先取得真实存储核验后的事实准备，再从真实 builder 重建冻结上下文，读取并验证原 Candidate intent、record、input、prose 和 Provider provenance，计算质量，最后重新执行原准备以拒绝期间发生的作者编辑、审核变化和来源变化。调用者不能提交蓝图、场景声明、完整准备或质量报告。

实际 `ChapterCandidateExecutor` 的 1.1 契约生成一份完整正文响应，持久记录不含分场景 draft/span。原计划中的 `candidate_scene_and_blueprint_evidence` 待办因此分成两个实际判断：对该契约明确标注场景证据不适用；对最终正文执行冻结蓝图的情节/结尾检查。不能伪造“所有节拍已经由场景声明覆盖”的生成证据，也不要求整个章节候选去恢复从未生成的场景 artifact。

新增 `build_text_blueprint_coverage` 直接复用已有文本覆盖规则，对每个 required beat 和末尾1200字内的 ending_pressure 检查，拒绝缺失生成必需字段或重复节拍编号，输出 `whole_chapter_text_heuristic_v1` 来源。旧分场景 `build_blueprint_coverage` 及 scene evidence alignment 未改变；旧流程即便正文含齐所有节拍，没有真实声明仍缺覆盖。

`prepare_candidate_chapter_quality` 将上述报告交给原 `validate_chapter` 的 `story_project` 检查，连同连续性、空间、逻辑、正文完整性、事实状态重放、本地审读和独立 review gate 汇总。正文采用最终作者修订；原候选仅提供来源证据，不能继承生成时的通过状态。质量 evidence 归档冻结 blueprint、candidate intent/record/input/prose hashes、完整检查结果及规则包，与前一层事实准备绑定。

standard 现在可得到 `candidate_quality_checked`、`quality_coverage_complete=true`、无未执行检查；发现缺陷时 `local_quality_passed=false`。这表示该版本标准策略已执行完整，不证明语义准确性、文学质量或上线收益。文本覆盖仍是既有词项/相邻汉字的启发式，误报漏报和更强语义策略在 T2.4/T8 验证。strict 要求模型校验，当前仍明确 `pending_gates=['llm_validator']`、不接受。所有结果保持 `publication_authorized=false`；上层必须在锁内重验并通过正式事务才可发布。

旧 `prepare_reviewed_chapter_quality` 仍是仅局部检查的内部接口，保留待办，不能误当完整 Candidate 质量门。两个输出版本明确区分。实际质量检查保留 `FinalArtifactIntegrityGate` 的真实时间戳，因此每次评估的 `quality_sha256` 可以不同；幂等请求来自已绑定的最终输入，不能以包含评估时刻的质量报告哈希作为重试键。

## 验证

- `.tmp/stage-c-candidate-quality-regression.xml`：57 passed、13 subtests passed；13项新 Candidate 质量测试、18项既有局部质量、21项服务准备、5项覆盖检查（其中2项新增）。覆盖真实持久草稿/作者确认/正式格式内部 builder，正常稿通过，删除必需情节后拒绝，同一原 Candidate 不继承旧通过结果，strict缺模型阻断，输入/正文/record在检查中损坏、作者并发改稿、大纲变化均拒绝；全文含结尾要求但末尾窗口不含时失败。
- `.tmp/stage-c-candidate-quality-scene-gates.xml`：24 passed；既有分场景 repair evidence、runtime review gate、quality decision 相关回归。
- 初轮 `.tmp/stage-c-candidate-quality-initial.xml`：12 passed / 1 failed；正常稿已通过质量门，但重复评估测试误要求真实检查时间戳相同。`.tmp/stage-c-candidate-quality-repeat.xml` 定位仅时间戳及报告hash不同。最终测试保留时间戳、比较完整其余报告与请求绑定。未冻结时钟或删除生产证据，失败XML保留。

新增15项测试。主session20609初轮失败后终止，92405最终exit0；场景/gate回归exit0。无Provider调用、真实作品写入、服务/UI启动；无需本轮浏览器或全套pytest。全部测试使用 `.tmp` 合成数据。协作代理本轮因额度失败，根代理直接完成，无独立代理复核结论可宣称。

## 下一步

质量这一项无需再从零调查。继续完成锁内正式提交与实际 v3 路径：作品所有权/DB锁、原意图识别、pending/回滚幂等、marker恢复及正式章节事务；联动新版上下文/Candidate/read-set/writer/recovery/job，完成真正升级v3后的生成—作者审核—提交—下一章冷读。当前普通身份仍只支持1/2；不能单独提高全局writer契约而让旧writer写新版作品。真实升级依旧等待维护、未知Provider/事务处理、T7.1备份和T2.2接入。API/UI、完整T3.2验收及后续C–F任务未完成。
