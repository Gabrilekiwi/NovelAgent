# Durable Delivery Queue 与下一章准入

本模块实现本地提交之后的独立外部投递。它在 Commit 11 中仍位于 shadow/兼容路径：现有 Memory writer 与默认真实写回行为不变，直到严格模式在后续提交显式激活。

## 边界

- StoryProject、Memory V2、Snapshot 和本地 RunRecord 属于本地事务，不属于 Delivery。
- file export、Notion 等独立目标在 Publication Receipt 验证后进入 Delivery Queue。
- 外部投递失败不回滚已提交正文，但任何未成功的 `required` job 都会阻断下一章。
- 配置 writer 时默认 policy 为 `required`；`best_effort` 只能显式选择；没有投递目标时才使用 `not_required`。

## 持久结构

默认目录为 `.tmp/runtime/deliveries`；StoryProject 使用 `<book>/.novelagent/runtime/deliveries`。

```text
deliveries/
  jobs/<job-id>.json
  attempts/<job-id>/<attempt-id>.json
```

`DeliveryJob` 保存 book/run、Publication Receipt hash、稳定 operation id、目标、payload hash、policy、状态和当前 lease。enqueue 对相同 immutable 内容幂等；相同 job id 对应不同内容时进入 `conflict`。

`DeliveryAttemptReceipt` 只保存 payload hash、阶段、结果、远端引用和脱敏诊断，不保存完整 payload、正文或凭据。目标配置也拒绝持久化 API key、token、authorization 和 secret。

状态固定为：

```text
not_required
pending -> delivering -> succeeded
                      -> retryable_failed
                      -> permanent_failed
                      -> uncertain
                      -> conflict
                      -> cancelled
```

`delivering` 必须有 worker id、attempt id、获取时间、过期时间和 phase。同一 job 同时只能有一个有效 lease。显式 reconcile 可以接管过期 lease：

- 远端 mutation 边界之前崩溃，转为 `retryable_failed`，可再次单次 attempt。
- 已记录 `remote_mutation_started` 后崩溃，转为 `uncertain`，后续只允许查询确认。
- attempt receipt 已落盘但 job 尚未 finalize 时，优先用 receipt 恢复，不重复调用 adapter。

Commit 11 不提供通用自动重试；一次命令只执行一次 attempt。Provider retry 由后续统一机制负责。

## Notion 协议

Notion job 使用由 book id、run id、job id 和 payload hash 派生的稳定 operation id，并同时保存稳定 Memory ID 和 payload hash。

每次 attempt 的顺序为：

1. required job 校验已捕获的 database schema、property 类型和字段长度。
2. 完整分页查询 database。
3. 唯一且 operation id、Memory ID、payload hash 全部一致时，直接 `succeeded`。
4. 多条命中或身份/内容不一致时，进入 `conflict`。
5. 仅当确认不存在且 job 不处于 query-only 状态时，记录 remote mutation phase，再执行一次 POST。
6. POST 必须返回 page id，并再次完整分页 readback；超时、缺少 page id 或 readback 不可见均进入 `uncertain`。

`uncertain` reconcile 永不自动 POST。`--resolve-delivery JOB_ID --confirmed-absent` 只有在 quarantine/read-after 窗口结束并再次完整查询仍不存在后，才把 job 恢复为 `pending`；下一次显式 reconcile 才可重新 POST。未来任何重复或 payload 冲突都进入 `conflict`。

## 统一 ReadinessDecision

`ready_for_next_step` 是查询时派生值，不写入不可变 Final RunRecord：

```text
accepted
and Publication Receipt verified committed
and project identity matches
and every receipt-bound required DeliveryJob is succeeded
and next-step context preflight is valid
and current read-set digest equals preflight digest
```

Receipt 中声明的 job 不得缺失；队列 job 的 book/run、Receipt hash、payload hash、policy 和 target 必须与 Receipt 一致。否则即使 required job 集合看似为空，也不会得到 ready。

`NextStepContextPreflight` 必须逐项证明：下一章细纲唯一、StoryProject sources 当前有效、project identity 一致、parser qualified、没有 blocking conflicts。`valid` 必须与五项检查和 conflicts 列表严格一致。Provider 调用前还必须用 `assert_provider_consumes_readiness_context` 比较实际 context digest；预检后漂移立即停止，不自动重调 Provider。

## 运维命令

```powershell
python main.py --reconcile-deliveries [--run-id RUN_ID]
python main.py --inspect-delivery JOB_ID --output-json
python main.py --resolve-delivery JOB_ID --confirmed-absent
python main.py --delivery-policy required|best-effort
```

可用 `--delivery-dir` 指定队列目录，`--delivery-worker-id` 指定 lease worker。required Notion reconcile 需要有效环境凭据，并通过 `--notion-delivery-schema PATH` 提供预检捕获的 database schema JSON。

`--reconcile-deliveries` 在仍有 required job 未成功时退出码为 1；best-effort 失败不会单独阻断。检查和 reconcile 命令都不会启动生成执行器。
