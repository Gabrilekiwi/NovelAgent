# T5.2 Provider 预算依据

2026-09-10查询并实际打开 [OpenAI Chat Completions 官方请求文档](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。未查询或使用账户密钥，未调用 Provider。

官方文档说明 `max_completion_tokens` 覆盖可见输出和 reasoning tokens；`max_tokens` 已弃用且不适用于 o 系列。service_tier 默认 auto，响应实际档位可能不同于请求。当前文档还包含 `prompt_tokens_details.cache_write_tokens`，以及默认 implicit、30m TTL 的 prompt_cache_options。不能沿用“OpenAI 不存在缓存写费”的旧假设。

本项目使用固定价格表做金额预检前，适配器必须约束并绑定：实际端点、模型、输出计费上限、可计费输入上界、服务档位和适用缓存费率/TTL。来源中未给出适用于任意兼容端点和任意未知模型的通用输入上界，因此不能从校准token估算推导保证金额。

本次修正调用报告把 `prompt_tokens_details.cache_write_tokens` 纳入缓存写入用量；与旧别名冲突时标记未知。现有预算契约只支持 5m/1h 缓存写入价格，不把未支持的非零 30m 用量变成零或直接结算；保留预留并暂停。后续仍需完善所选真实 Provider 的受信适配器与冻结规则。

这些是当前接入需要的约束，不构成已经支持真实金额止损的验收结论。价格仍只来自测试合成值。


## 2026-11-03：当前代码与官方契约重新核对

本节更新当前缺口，不重复把早期尚未实施的内容当作现在全部欠项。已实际读取当前价格契约、ChapterBudgetStore、SDK适配器和runtime：精确金额计算、跨execution章账本、预留/结算/未知占用、持久主动计时及默认工作台注入已有实现；OpenAI/Anthropic SDK实际调用边界仍未提供input_bound。`ChapterBudgetStore._plan`在amount_limit非空而缺可信输入上界时明确拒绝，故尚不支持宣称真实金额止损。

2026-11-03按OpenAI Docs技能先检索并实际打开官方来源：

- https://developers.openai.com/api/docs/models/gpt-4.1-mini
- https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create

模型页当前把gpt-4.1-mini及其2025-04-14快照标为Deprecated。这是仓库fallback默认模型族，不代表用户实际配置或账户可用性；没有查询账户，也没有替换默认模型。其模型页给出的上下文/输出能力不能自动充当任意兼容端点、别名或第三方服务的计费保证。

请求文档确认max_completion_tokens覆盖可见与推理输出，max_tokens为旧字段且有模型适用限制。service_tier缺省auto，响应实际档位可能与请求不同；冻结单一价格前需要约束请求并记录实际响应档位。prompt_cache_options包含缓存模式/TTL，完整计费绑定不能由旧5m/1h费率推测未支持缓存的价格。现有不明费用继续保留预留，不按零费用释放。

下一实施边界：受信Provider能力/计费契约须绑定实际endpoint指纹、精确模型/快照、允许的请求形状、输出计费上限、单一价格档位、全部输入的可解释上界与缓存规则；请求摘要必须与最终SDK请求一致。不得硬编码已弃用模型作为新的唯一支持路线，不静默切换用户模型，不把字数/token估算包装成Provider保证。先用替身SDK完成金额预留、未知计费、失败与重启验收，真实调用仍等待独立Provider/金额/阅读授权。本轮仅查询公开文档，没有读密钥、没有Provider调用或写真实书。


请求计划实现来源补充（2026-11-03）：实际检索并打开 https://developers.openai.com/api/docs/models/gpt-5.4-mini 与 https://developers.openai.com/api/docs/guides/conversation-state ，并再次核对Chat Completions请求页。精确模型条目和能力上限用于可选的纯文本请求计划；未采用搜索摘要作为计费保证、未导入实价或替换用户模型。固定能力与最终请求绑定的实现和边界测试见t52-budget-implementation最新节；SDK实际金额接入仍未开启。
