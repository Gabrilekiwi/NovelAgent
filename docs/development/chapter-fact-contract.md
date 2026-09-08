# T2.1：章节事实契约与只读准备

2026-09-08。用于阶段B的确定性技术验证；不构成正式提交或旧书下章读回。

## 同源契约

`schemas/chapter_fact_payloads.schema.json` 是七种 proposal 的字段、必填项、操作与事件角色定义来源。`chapter_fact_contract.py` 向模型提示和本地规范化提供同一份定义；已有外层提案与审核记录仍为1.0。通用schema校验器不支持oneOf，因此按kind选择同名属性，校验 `{kind: proposal}`，没有扩展全项目schema框架。

`examples/chapter_fact_review/seven_kinds.json` 保存合成正文与完整七类输出。提示同时展示七类定义和该样本，明确样本与作者当前正文的边界。该例已经实际经过提案解析、证据定位、作者决定检查和只读准备；不是新模型效果评估。

| 种类 | 模型提出的核心字段 | 程序读取或计算 |
|---|---|---|
| event | 类型、状态、发生地点、在场/远程角色、运输物与逻辑关联 | 事件ID、正文证据位置、同组依赖 |
| character_state | entity_id、canonical_name、field、after、source_event_key | 已有字段before与稳定身份核对 |
| relationship | relationship_id、两端实体、relation_type、field、after、source_event_key | 已有关系身份、字段before |
| roster | roster_id、add/remove、member_ids、source_event_key | 原人数、增减数、结果人数；转为既有join/leave reducer |
| location | entity_id、from_location、to_location、source_event_key | 已有当前位置before；核对正文声明的出发地 |
| inventory | owner_id、item_id、非零整数delta、source_event_key | 唯一台账的原数量、结果数量、非负校验 |
| counter | counter_id、increment/set、value、source_event_key | 原值、增量、结果值；复用计数边界规则 |

`from_location`是正文中提出的出发地，可为null；它不能替代可信当前位置。库存缺项、计数未定义、位置未知、名单或字段缺基线均不能默认补零。明确存在的零数量可以参与计算。新增实体和基线补充由T2.2接入，身份及source_tier等元数据不能借普通状态字段修改。

## 纯准备入口

`prepare_confirmed_fact_changes(facts=..., chapter_text=..., base_state=...)`消费已经绑定的状态快照和有明确作者决定的审核卡片。它不读取作品、不写文件或数据库、不调用Provider。

处理规则：

1. 全部卡片必须明确接受或拒绝；接受的变化不得依赖被拒事件。
2. 同一事件、目标和变化不会因换摘要或长短引用而重复计入。相同事件载荷的重叠引用不能伪造第二个来源；非重叠的独立事件可以重复相同增量。
3. 每个目标的变化按原文证据位置计算；重叠的证据范围不能决定顺序，直接阻断。输入卡片数组反转不改变结果。第二次变化的before来自第一次计算后的临时状态。
4. 从明确基线生成before/after、数量、领域delta和事件依赖，再调用现有权威状态校验器及名单reducer。既有计数上下界、身份规则、名单合法性继续生效，没有改变旧引擎的状态来源优先级。
5. 返回正文/基线哈希、state_delta、computed_changes、state_after、event_dependencies；输入保持不变。

## 验收与限制

用例覆盖七类混合、真实零库存、缺数量和字段基线、set不能新建计数基线、数量非法、证据改稿失效、重复事实/事件、六类被拒事件依赖、明确与歧义顺序、身份字段越权和已有计数上界。结果与哈希记录于`stage-b-progress.json`，原始pytest XML在`.tmp/stage-b`。

本入口的base_state必须由后续T3.2使用真实书籍来源、版本和read-set绑定，不能把任意传入字典的哈希当作来源可信证明。T3.0/D1尚未解决旧书人工初始设定与当前状态如何共存，T2.1输出不能绕过D1直接发布。

证据逐字匹配和位置排序只提供结构检查，不能证明引用在语义上支持候选，也不能证明倒叙文本的实际时间关系；T8.0/T2.4的风险样例与策略仍需检验。空事实集合不证明正文没有事实变化，最终人工明确确认及自动检查由T2.3/T2.4约束。

本次补强保持合法v1载荷形状不变；原来含重复事件或变化的无效提案/审核可能会被新校验拒绝，保留原始模型建议用于诊断，不静默修写历史证据。网页事实审核UI、正式事务、EA完整对等和可写恢复不在本项完成声明内。
