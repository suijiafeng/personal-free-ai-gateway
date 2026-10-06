# ADR：用官方 SDK 的薄桥补齐流式拒绝语义

日期：2026-10-06。决策：采用显式、受限的 `openai_sdk` 适配选择；不升级、不修改 LiteLLM 1.104.0 或 OpenAI Python 2.54.0 的安装文件。

## 为什么需要这个扩展

旧版审查已通过真实 TCP 模拟器重现：原生 OpenAI/Groq 流处理会删除独立的 refusal-only delta，即使前面已有正文。常规 `CustomLogger` typed iterator/deployment hooks 在删除之后才接触对象，无法恢复其文本或判断是否曾拒绝。正常 finish_reason=stop 也不能证明没有丢失拒绝。原生用量还可能由 LiteLLM 估算或补默认值。

本次再次查看[当前上游 streaming_handler](https://github.com/BerriAI/litellm/blob/main/litellm/litellm_core_utils/streaming_handler.py)，未取得可验证的已发布修复。没有修改供应商包、发布 issue、套用未发布 patch，或声称更新版本已经解决。

这仍然是新增适配代码，不能称为“没有供应商适配维护成本”。它负责一个窄字段映射和安全生命周期边界，不重写供应商 SDK、HTTP/SSE 解析器或路由器。维护者必须重跑这些兼容测试；不能据此承诺所有 OpenAI-compatible 服务都兼容。

## 采用的受支持边界

- LiteLLM 的[CustomLLM 注册与 astreaming / GenericStreamingChunk 接口](https://docs.litellm.ai/docs/providers/custom_llm_server)负责插件接入；Native Router、Proxy、受限密钥及数据库仍是原有实现。
- [GenericStreamingChunk 的版本化定义](https://github.com/BerriAI/litellm/blob/v1.104.0/litellm/types/utils.py)提供 provider_specific_fields 扩展；本桥用其传递 refusal-only 字段和保持真实空事件，绝不添加虚构正文标记。已有 iterator hook 将拒绝字段恢复到公开 delta.refusal。
- [OpenAI Python 2.54.0 官方接口](https://github.com/openai/openai-python/blob/v2.54.0/README.md)完成请求、连接、JSON/SSE 解码、typed chunk 和关闭。with_raw_response 用于读取响应头；公开 parse(to=dict[str, Any]) 与 parse(to=AsyncStream[dict[str, Any]]) 返回已解码字典，再严格检查数字类型。不能使用 SDK 的 CompletionUsage 模型建立来源，因为它会把 bool、字符串及浮点转成整数。不得用已经过 LiteLLM 规范化的 Usage 建立来源。
- SDK max_retries=0；HTTP client 禁止跳转，以免已批准的地址/凭据转向另一目标。网络仍使用环境的既有代理控制。没有添加自定义 transport。

公开 parse(to=...) 的自定义类型和流泛型分支见[2.54.0 方法文档与实现](https://github.com/openai/openai-python/blob/v2.54.0/src/openai/_legacy_response.py)。使用完整 dict[str, Any] 类型参数；此 pin 的裸 dict 会触发上游类型构造错误。这里没有接触 SDK 私有成员，也没有重新解析事件字节。SDK 常规 JSON 解码并非原始字节或重复键取证。

## 策略与实际目标保持一致

策略保存真实 provider、model、api_base、credential_env；adapter 是独立字段，默认 native。编译后的模型为 gateway_openai_sdk/provider/model，供应商身份不会被插件前缀抹掉。

这里的 model / 实际目标指已批准配置中真正发送的供应商与请求模型标识，不是供应商内部解析后的物理版本。桥的公开 model 也采用此配置目标；未宣称已验证 provider 返回的任意 resolved model 字符串，不将这些未审核字符串加入诊断日志。如未来需要逐次物理版本证据，必须另做可信映射与留存审核。

逐尝试 Guard 只接受精确编译模型及其合法去前缀形式。桥再检查当前请求对象、attempt、deployment、已取得的池、完整消息、实际地址、输出上限和 production 凭据引用。任何不匹配先失败，不发送网络请求。未知参数不能静默删除。Native Proxy 的内部 stream_options 只接受精确的 include_usage=true；这不开放消费端同名参数。

LiteLLM 的 CustomLLM 映射把公开 max_completion_tokens 转成 max_tokens。本桥核对数值等于已验证请求，按相同上限交给官方 SDK。具体模型是否按预期执行该上限，仍是实际渠道资格测试的一部分。没有假定默认参数一致，更不接受任意 extra_body、地址、工具、隐藏会话或服务等级。

production 的 native adapter 不允许 streaming=true：这是 schema 护栏，而非只在示例中写 false。mock 模式仍保留原生路径，供旧问题回归与对照使用。

## 两个候选的范围

- Groq 官方[OpenAI 兼容接口](https://console.groq.com/docs/openai)位于 https://api.groq.com/openai/v1。其[参数说明](https://console.groq.com/docs/api-reference)记录 max_tokens 与 max_completion_tokens；本桥只支持纯文本单 choice，不开放 Groq Compound、多模型内部代理或工具。
- Gemini 官方[OpenAI 兼容文档](https://ai.google.dev/gemini-api/docs/openai)明确展示 https://generativelanguage.googleapis.com/v1beta/openai/、OpenAI SDK 和流式 Chat Completions，同时提示此兼容层处于 beta。本桥按此精确 endpoint 提供可配置候选，不将 Vertex AI 文档当作此 API 的实测证明。

生产示例已选择这两个明确端点与 adapter，但所有实际渠道仍 disabled、免费资格 unknown、streaming=false、模型 REVIEW_REQUIRED。合成测试验证的是传输路径及适配规则；不能证明真实账户免费、具体模型行为、区域条款或供应商取消计量。

## 流式契约

1. 正文按到达顺序输出。role / empty / refusal-only 都会建立首事件后的禁止新尝试边界。
2. 独立 refusal、正文后的独立 refusal、正文与拒绝同块、终态与拒绝同块均保留拒绝语义。真实空回答在明确 stop 后允许完成；不再用原生缺陷的空流拒绝补丁误伤此路径。
3. 仅 stop、length、content_filter 可作为终态。工具、多 choice、非文本内容和非预期额外输出字段明确失败。
4. 只暂存终态标志直到 SDK 消费结束，不缓冲正文。终态后的断流、异常或额外正文不能先发出成功样式的 stop。没有真实上游终态的干净 EOF 也失败。
5. 首事件前仅 Native Router 可按现有资格/次数预算选择备用；首事件后禁止拼接、重放和透明切换。取消时尝试关闭 SDK；本地关闭不能证明供应商没有继续计量。
6. 真实 usage 的三个完整、非负整数才记录 upstream_observed；缺失、null、部分字段保持 unknown，显式 0/0/0 保留。相互冲突的用量明确失败。不得回填 LiteLLM 估算。
7. 公共流仍省略 usage；初始 x-usage-source 仍 unknown，因为 final usage 尚未到达。请求结束后的受保护诊断可给出已观察数值。没有扩大公开 stream_options 契约，也没有把本地估算包装成上游用量。

## 版本、许可与安全

本次没有增加新版本或新下载；openai==2.54.0 已在现有哈希锁文件中，现在另加为直接依赖。LiteLLM 的此接口位于 [MIT 覆盖范围](https://github.com/BerriAI/litellm/blob/v1.104.0/LICENSE)；OpenAI SDK 为 [Apache-2.0](https://github.com/openai/openai-python/blob/v2.54.0/LICENSE)。原 [mixed-license 与供应链审查](../evidence/supply-chain.md)仍适用，本桥不改变 enterprise 包的许可边界。

同日既有 dependency-audit.json 覆盖这两个未变更的 pin；这不是新扫过所有 OS、容器或隐藏漏洞的声明。release 仍需完整供应链和实际运行环境检查。

## 验证与维护

- tests/contract/test_sdk_bridge.py：真实 loopback TCP + Native Router + 原 Guard + 官方 SDK。
- tests/test_sdk_bridge_config.py：策略编译、实际 provider 保留、精确端点、禁止原生生产 streaming、审计配置。
- tests/integration/test_sdk_proxy_postgres.py：原生 Proxy、实际 PostgreSQL、受限消费密钥、流 wire 输出和完成记录。继承共同安全用例只是不同适配路径复跑，不当成新增功能数量。
- evidence/sdk-router-config-results.xml 和 evidence/sdk-proxy-postgres-results.xml：本轮结果；以最终整仓回归与独立审查为最终验收。

升级任一 pin、修改字段映射、允许新 endpoint/model/role/参数时，先扩展模拟器和原生 Proxy 测试，再重新审核真实渠道。不得通过禁用新检查绕过兼容失败。

## 原生 Proxy 的进程隔离要求

LiteLLM 1.104.0 关闭一次 lifespan 后仍保留全局 callback。实测同一 Python 进程再次构建不同配置的 Proxy，会重复登记同一个 Guard，进而在尚未调用上游前返回错误。bootstrap 因此明确限制每进程一次成功构建；配置发布、重启和不同适配器测试都启动新进程。canonical PostgreSQL runner 在两个独立 pytest 进程执行完整 native / SDK 测试并合并原始 JUnit，任一失败仍阻止通过。详见[精确复现与修复记录](../evidence/native-proxy-process-isolation.md)。

## 取消后的原生 key 槽恢复

端到端长序列测试发现，入口非流式总 deadline 会绕开部分原生回调而遗留每 key 并发槽。薄入口现以版本绑定、有界、幂等的方式补调用原生 slot-ID 清理方法，保留原 key 并发契约；失败明确阻断后续调用，不直接修改计数器。依据、限制和同 key 连续故障/恢复测试见[原生槽清理记录](../evidence/native-request-slot-cleanup.md)。
