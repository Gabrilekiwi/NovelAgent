# Unified Provider Retry

NovelAgent 的外部读取与模型调用统一通过 `RetryPolicy`。Provider SDK 的隐藏 retry 始终设为 0，所有可见重试、deadline、delay 和 attempt history 由应用层控制。

## Operation profiles

| Profile | 默认策略 | 用途 |
| --- | --- | --- |
| `model_read_generation` | 最多 3 attempts | OpenAI Director、生成、修复和 LLM validation |
| `claude_polish` | 最多 3 attempts | Claude polish |
| `notion_read_query` | 最多 3 attempts | Notion 完整分页读取与查询 |
| `notion_create` | 固定 1 attempt，禁止 generic retry | Notion page create |

model/read 默认 base delay 为 1 秒、max delay 为 8 秒、jitter 为 20%，总 deadline 为 180 秒。可用以下环境变量收紧运行策略：

```text
PROVIDER_MAX_ATTEMPTS=3
PROVIDER_RETRY_BASE_DELAY_SECONDS=1
PROVIDER_RETRY_MAX_DELAY_SECONDS=8
PROVIDER_RETRY_JITTER_RATIO=0.2
PROVIDER_RETRY_DEADLINE_SECONDS=180
```

`PROVIDER_MAX_ATTEMPTS` 的硬上限为 10；超出上限的配置会被收紧到 10，避免错误配置导致无界调用。

每次 SDK request timeout 会被限制在 retry 总 deadline 内。调用方还可向 `RetryPolicy.execute()` 或 provider client 注入 `budget_remaining_seconds`，使下一次 delay 同时受运行级剩余预算约束。

旧 `OPENAI_MAX_RETRIES` 暂时兼容一个版本：当新的 `PROVIDER_MAX_ATTEMPTS` 未配置时，映射为 `max_attempts=max_retries+1`，并发出 `FutureWarning`。它不再传给 OpenAI SDK；OpenAI 和 Anthropic SDK 的 `max_retries` 都固定为 0。

## 可重试分类

只有下列失败进入下一次 attempt：

- connection；
- timeout；
- HTTP 429；
- 明确的 500、502、503、504。

认证/权限、其他 4xx、配置、schema、output contract 和普通 provider response 错误不重试。分类会检查完整异常链，外层 401/403 不会被内层 socket cause 错判为 connection。

`Retry-After` 优先于指数 backoff，支持秒数和 HTTP date，但不会越过剩余 deadline。deadline 或运行预算不足以容纳 delay 时立即停止，不先 sleep。

## Streaming guard

OpenAI 与 Claude 的流式读取在异常时记录 `partial_content_received`：

- 尚未收到任何文本时，可按错误分类重试；
- 已收到任意文本后，不拼接、不重放，立即以 `partial_content_received` 停止。

这样不会把两个 provider attempts 的半段内容合并成一份看似完整的正文。

## Telemetry

每个 operation 生成 schema-checked `ProviderRetryReport`：profile、max attempts、总 elapsed、stop reason，以及逐次 attempt 的分类、elapsed、delay、Retry-After 和 partial-content 标记。history 只记录错误类型与分类，不记录异常 message、凭据、prompt 或正文。

Executor 会把当前 Director 或 workflow action 产生的报告写入 `provider_attempts`；失败的 `ModelCallError` 同时携带 attempt history 和 stop reason。LLM Validator 的 quality metadata 使用同一份 history，不再固定伪造为一次成功。

Provider smoke 保留旧命令行参数，但会把显式 retry 限制映射到统一环境策略；已经由统一 policy 完成 attempts 的错误不会再被 smoke wrapper 二次重试。Notion create 在 runtime、Delivery Queue 和 provider smoke 中都不进入通用自动重试。
