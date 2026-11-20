# T5.1 调用用量与费用：实施起点

2026-09-10，只读调查结论。T4.1已验收；本文件不表示T5.1已实现。

## 已确认的事实

1. 物理调用证据已经存在：`core/model_calls.py::ModelCallStore` 逐次保存 Intent 和 Receipt，load_receipt 会验证哈希和 Intent 关联。`core/model_call_runtime.py::execute_attempt` 使用 `call_id-a{attempt_number}`，重放原 Receipt，不建立新的物理调用。
2. Intent 含 provider、请求 model、stage、created_at、预算预留；Receipt 含 usage、actual_model、endpoint_type、status、received_at 和凭证关联。成功、部分响应失败、确定未发送、Provider明确拒绝及缺回执要分别展示。
3. OpenAI/Claude 适配器保存实际 SDK usage 映射，没有把所有缺项补成零。旧字符串响应可以产生空 usage。运行时的部分终态失败会合成零 token，这不等于账单已结算为零。
4. `core/context_budget.py` 的 `estimated_cost` 默认从 0.0 累加；当前调用 runtime 的预算预留只有 token 数量，没有价格或币种依据。不得把这个旧默认零值直接升级为实际零费用。
5. `ModelCallReceipt` 1.0 契约没有明确费用来源/币种或调用时价格快照；usage 虽允许附加字段，也不能任意把某个 cost 数字当成结算账单。需要为新记录明确来源约定，同时保留旧记录不改写。
6. 证据位置为 `.novelagent/runtime/runs/executions/execution_*/model_calls`，单个 attempt_id 在不同 execution 中可以相同。汇总键必须包括执行身份；不能只按 attempt_id 去重，也不能按页面刷新/逻辑重放次数加总。
7. 旧工作台 `/history` 只收集普通任务与 `chapter_*.json` 摘要，尚无物理调用列表。新版作品的 `job_history` 能力仍为 false；不能只给旧历史页加一列就声称新版逐章链路可追溯。

## 接下来的实现顺序

1. 从核验后的 Intent/Receipt 做只读投影，显示执行、attempt、阶段、请求/实际模型、端点类型、结果和原时间。未知、正在执行/无回执与明确失败保留独立状态，不能悄悄丢弃损坏证据或失败调用。
2. 区分 Provider 返回的 token、程序估算的 token、预算预留及未知。不能把输出上限算成实际输出，也不能把缺失字段默认为零。缓存/推理等明细保留来源，避免和总量重复相加。
3. 费用使用 actual/estimated/unknown：actual 必须有明确金额、币种和账单来源；estimated 必须有本次适用的价格/计算来源；缺少这些依据为 unknown。合计按币种分组，包含未知项时只能给已知小计，不能显示为完整总额。新证据来源的持久方式需先与已有不可变契约核对，不重写旧哈希。
4. 将同一只读投影接到新版逐章生成/提取及旧执行的可达界面，展示按 attempt 的明细。避免另建第二本可漂移的费用账。
5. 验证缺 usage、历史无依据零值、Provider usage、明确失败、部分响应、无回执、跨 execution 同 attempt_id、同 Receipt 重放、损坏证据及刷新后一致；再做真实浏览器的展开明细/来源/未知提示验证。

## 保持的边界

- T5.1负责证据与展示；费用预算硬上限属于T5.2，自动预算留给T5.3，不把展示数字直接作为执行许可。
- 不联网猜测当前模型价格，不调用真实Provider，不修改真实书。若需要价格估算，保存明确适用的价格来源与币种；没有配置则诚实显示未知。
- T5.1后继续T4.2，再D–F；不是只完成这两项就结束完整开发目标。
