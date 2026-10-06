# 用量、响应头与拒绝的来源边界

固定 LiteLLM 1.104.0、官方 OpenAI Python SDK 2.54.0。不能因为最终 `Usage` 对象非空就把它当成上游实测：原生层可能在缺失时补零或估算。

## 两条明确路径

| 路径 | 用量来源 | 流式资格 |
| --- | --- | --- |
| adapter=native | 官方 log_post_api_call 的规范化前原始 JSON；仅已认证非流式形状 | 生产禁止，因独立 refusal 在 hook 前丢失 |
| adapter=openai_sdk | 固定官方 SDK 从上游直接解析的 typed response/chunk，严格绑定请求及尝试 | 支持窄文本契约，已在原生 Proxy/PG/TCP 合成链路验证 |

窄适配仅填补已有可复现缺口，通过官方 CustomLLM/GenericStreamingChunk 扩展注册。没有改 LiteLLM 安装源码、重写 HTTP/SSE 解析器或第二套路由；维护责任和完整边界见 [架构决策](adr-sdk-stream-bridge.md)。

## 什么才记为实测

仅提取 prompt_tokens、completion_tokens、total_tokens 三项；全部必须由上游明确提供，且是非负整数，bool 不算整数。显式 0/0/0 合法；缺失、null、部分、负值、浮点、字符串或未知 schema 保持 `usage=null`、`usage_source=unknown`。原生回调若给出原始 JSON 文本，额外拒绝重复键；SDK 路径使用其公开已解码对象，JSON 解码行为由官方 SDK 负责，不宣称对原始重复键做了额外字节级验证。

不复制可选 nested token details，不保留原始响应、提示词、回答、拒绝文本、原始错误或无关响应头。原生规范化后的 ModelResponse、hidden usage、model_fields_set、成功回调或 DONE 都不能独立证明来源。

每次真正开始上游尝试时清除前次观察；同步回调和 SDK binding 均核对当前 deployment/attempt，防止并发请求或备用污染。SDK bridge拒绝冲突的多份用量数据。

非流式仅在验证通过后公开真实三项 usage，其他情况为空。SDK 流的有效数值写入最终私有诊断；公共流仍不开放 stream_options 或承诺最终 usage 块。流响应头在数据到达前发出，因此其早期 unknown 不能代替最终诊断。原生流无法认证时继续 unknown，不把“上游可能有值”猜成测量。

这是供应商报告的推理用量，不是独立 Token 审计、账户余额或账单零费用证明。

## 头部来源与额度

只允许六个 x-ratelimit-{limit,remaining,reset}-{requests,tokens} 以及 retry-after。大小写不敏感；控制字符、非 ASCII、过长或冲突字段丢弃。Authorization、Cookie、任意调试头及内部地址不保留。

- SDK 路径在官方 with_raw_response 边界取得非流式、流式与错误响应头，过滤后绑定当前尝试。每一组变化后的头最多持久化一次，不按 Token 反复写数据库。
- 旧原生 OpenAI/Groq 各路径保留头的方式不同；Groq 原生非流式成功头会丢失，所以该旧路径不更新余额。
- 对 Groq 的请求/日与 Token/分钟使用分别审核的映射；Gemini 未提供可认证余额时保持未知。Pacific 午夜规则不能凭空产生余额。
- Retry-After 只是可用性反馈；429 不等于余额为零。真实零余额必须有经范围/时效校验的 remaining=0。

## 拒绝与终止

SDK 路径保留单独 refusal-only、先正文后 refusal、同块 refusal+终态，以及合法空回答；只有明确的 stop/length/content_filter 经完整 SDK 读取后才输出终态。不接受 tools、未知输出角色、非文本内容、未知 finish_reason 或终态后的额外内容。

旧原生缺陷仍保留复现测试；它的“测试通过”只表示缺陷可重复，不能算完整拒绝验收。生产 validator 禁止 native+streaming，推荐 SDK 路径；不把旧路径自动重试成新请求，不以其他模型绕过拒绝。

证据分层见 `evidence/sdk-stream-bridge-review.md`、`tests/test_metadata.py` 和实际 Proxy/PG 专项。全部使用合成上游；真实账户/模型能力仍需现场资格核查。
