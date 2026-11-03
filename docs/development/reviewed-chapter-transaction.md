# T3.2：新版作品的正式章节事务

日期：2026-09-09。基于 `bbedefc` 工作区，内部事务已实现；完整共享服务、作品/DB所有权、重试意图、worker/API/UI仍未完成。不能将本组件通过等同于 T3.2 验收。

后续进度：共享提交后端的所有权、DB写事务、原意图与重试已在 [共享提交服务](shared-chapter-publication.md) 接入；最新待办以交接和stage-c-progress为准，本篇保留事务组件的验证范围。

## 实际提交内容

`prepare_reviewed_chapter_transaction` 接收服务端已核验的事实准备、请求与可信上下文，重新构建实际 identity 3.0 上下文，执行真实 Candidate 标准质量检查，再用现有 `PersistenceV2Transaction` 准备正式事务。调用者不能传入质量报告。strict缺少模型检查时拒绝准备；当前只接受完整通过的standard。

apply目标严格限定为当前已审核状态与本章规范化正文；publication目标包含同字节的状态/正文归档、完整请求准备、完整质量报告、最终记录及事务凭证。状态文档携带更新后的七类事实与committed_through；正文序列及该字段共同约束下一章游标。identity字节保持不变，未提高global writer contract。

新类型 `legacy_reviewed_chapter_commit_v1` 放在已审核状态的写入声明中。持久化层只有发现这一类型才调用新版事务核验；缺少、额外或错配目标，正文/状态/原始read-set/请求/质量/最终记录/凭证不一致均拒绝。普通 read-set verifier 仍不接纳新版身份，其他事务沿用原路径。

## 写入与恢复边界

准备与pre_apply验证完整已发布历史、真实身份、人工来源和当前游标。pre_marker时，当前状态已是新章结果、对应凭证尚未发布；专用验证器从已校验的事务before-image验证完整保留历史的唯一末端，再核验新状态严格等于原事实准备重放、归档等于apply字节，以及原read-set仅发生这两项声明写入。它不能选择更早的历史版本，因为全部保留凭证仍须形成同一唯一末端。

用于该阶段的父文档参数仅存在于私有history/identity函数；公共冷读始终要求当前文件对应已发布历史。提交阶段从正式journal读取并校验全部staged目标和状态backup，不凭prepare时的布尔结果授权。源文件在prepare后或写入后、marker前改变，会拒绝发布并由现有事务回滚本章。

故障恢复继续复用既有marker协议：marker前恢复before-image，marker后完成同一run的publication。不会再次生成正文。冷读identity 3.0的每一个chapter receipt还须具有正式类型、完整请求和规范路径的质量artifact，重新绑定事实重放、最终正文、read-set、上下文与最终记录。已发布请求索引可以从这条真实v3凭证链重建。

## 尚未完成

这个函数是内部事务组件，尚未取得覆盖整个请求的作品所有权或SQLite写事务；调用者必须在后续正式服务中完成登记资格、草稿/审核CAS、pending处置和锁内重验。持久化内部文件锁覆盖prepare与commit各自临界区，不代表覆盖两次调用之间的作者编辑窗口。当前不接公共API或普通worker，不宣称双击只产生一个逻辑请求、pending/回滚重试或完整数据库恢复已实现。

下一个实现点是原意图服务：在固定作品所有权和DB事务中先识别已发布/pending/回滚的原请求，再核验当前审核并调用本组件；记录原幂等键与请求绑定，恢复后从真实凭证重建索引。不能在marker前失败后擅自换run重新生成，也不能用带评估时间戳的quality hash替代幂等请求。

之后接统一recovery/job契约、preview/commit API/UI与真实浏览器，完成 T3.2 全套pytest/smoke验收。真实旧书升级仍等T7.1备份、T2.2接入及维护预检；本轮仅 `.tmp` 合成Strict作品和确定性生成，没有付费Provider或真实作品写入。复核代理额度错误，未宣称独立复核。

## 验证证据

- `.tmp/stage-c-v3-tx-regression.xml`：18 passed；真实正式提交与下一章新进程读取、五个故障位置、八类错误bundle、strict缺模型、两处来源并发变化及质量归档损坏。
- `.tmp/stage-c-v3-tx-persistence-regression.xml`：123 passed、100 subtests passed；原v2/v2.1持久化、实际升级、正式状态文档/历史、已发布请求索引。
- `.tmp/stage-c-v3-tx-entrypoint-regression.xml`：63 passed、3 subtests passed；新增两个staged质量/父文档backup损坏测试，新版候选链、D2格式门与既有EA运行/持久化回归。

合计204项测试及103项子测试通过，本轮新增20项测试。所有session98200、10040、17559已exit0，无活跃测试或服务。初轮session21763与 `.tmp/stage-c-v3-tx-initial.xml` 为1通过/5失败：实际提交通过，五个恢复均返回ok，但测试误把恢复接口的对象当成列表；改为真实transaction_count/transactions字段后重跑通过。早期失败文件保留，不计作通过证据。哈希与文件清单见 `stage-c-progress.json`。

Git本地检查点仍未保存：此前 `git add` 被自动审批因工作区额度耗尽拒绝，HEAD为 `bbedefc`，未绕过；本轮与前轮修改均保留在工作区。未执行全套pytest/smoke或浏览器，待T3.2完整验收点执行。
