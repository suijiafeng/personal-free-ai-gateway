# SDK 薄桥验证记录

2026-10-06。只使用合成正文、合成凭据及 loopback HTTP/PostgreSQL。

## 结论

已找到并实现受支持的薄扩展：LiteLLM CustomLLM + 已锁定 OpenAI Python SDK + GenericStreamingChunk.provider_specific_fields。原生 SDK/Router/Proxy 文件均未修改。原生适配器的 refusal-only 缺陷依然存在；使用新 adapter 才能获得新保证。

初步研究中的 16 个真实 TCP + 原 Guard 场景全部通过。正式实现最终扩大到 50 个 Router/schema 场景全部通过。新增严格数字类型用例曾发现SDK typed coercion，最终已用公开parse(to=dict[str, Any])与parse(to=AsyncStream[dict[str, Any]])修复并复测。完整原生 Proxy/PostgreSQL 适配路径 35 项及 10 个 subtests 通过：其中 26 项复跑共同安全义务，9 项是本桥新增端到端专项，不把重复执行当成新增功能。以整仓最终回归为交付验收。

## 未修改的安装文件 SHA-256

- litellm 1.104.0 integrations/custom_logger.py: d0260a889c370eb764c05fcd315fc3eb594a2b1dd708e57a94209ad7d803303a
- litellm 1.104.0 litellm_core_utils/streaming_handler.py: a0891d3c589175789eb527fa80a89cdcb5b77dcbab05640ac87234e560819c4f
- litellm 1.104.0 llms/custom_llm.py: 9aed45ba33add60e9fc66abb06f2079435afc4d8f533e8196292d466ce9927bc
- openai 2.54.0 _streaming.py: 3178ebb721f4ff3cda6d479520ba2bddcdc7096e2a99827c6a0bc150fe44534a

这些摘要证明本次读取的关键文件与旧基线/本轮 pin 一致，不是整个供应链可复现构建证明。

## 可复跑入口

从仓库根使用 requirements.lock 所确定的虚拟环境：

    python -m pytest -q tests/contract/test_sdk_bridge.py tests/test_sdk_bridge_config.py

完整数据库夹具由 tests/run_postgres_suite.py 建立；有独立 PostgreSQL17 的情况下，可在同一 namespace 设置 GATEWAY_TEST_DATABASE_URL 后运行 tests/integration/test_sdk_proxy_postgres.py。不得指向现有生产库，测试会创建并撤销合成受限密钥。

完整架构决策与外部资格边界见 [ADR](../docs/adr-sdk-stream-bridge.md)。

## 最终专项结果

- Router / schema：50 passed，22 dependency warnings，21.13 seconds；验证直接SDK解码字典不强制转整数、冲突用量拒绝、官方声明audio/annotations非文本字段拒绝、两provider身份和逐attempt绑定。
- Native Proxy + fresh PostgreSQL17：35 passed，10 subtests passed，12 dependency warnings，35.03 seconds。
- 模拟器覆盖首事件前、role后、正文后中断；仅首事件前能备用。总deadline与取消均关闭本地流并释放池。真实空流、拒绝与terminal同块、独立拒绝在正文前/后均符合契约。SDK final marker保留至读取结束，terminal后传输中断不发送stop或DONE。
- Native Proxy自动加入内部stream_options.include_usage=true，实测后增加严格匹配，仅此结构受支持；公共契约仍拒绝消费端stream_options。
- Quota响应头按真实attempt持久化；已观察耗尽只挡住确认相关池。
- 依赖警告包括Pydantic弃用/ReadOnly提示，以及原生router_cooldown_event_callback未await提示。未把警告当作通过证据；相关fallback/冷却/资源释放由独立状态断言验证。未修改vendor代码消除警告。
- 文本记录见sdk-router-config-console.txt、sdk-proxy-postgres-console.txt；JUnit见同前缀results.xml。

在这些合成结果基础上仍不能声称实际Groq/Gemini模型、账户免费资格、供应商计量取消或生产容器已被验证。

## 后续生命周期补强

随后在长序列整仓运行中发现并修复两类真实集成问题：原生Proxy重复lifespan登记重复Guard，以及非流式取消遗漏原生key槽释放。完整原因、边界及复现分别见native-proxy-process-isolation.md、native-request-slot-cleanup.md。新SDK模块41项加10个subtests通过；恢复出的旧key也通过三轮deadline→成功，并保持原maxparallel=2。最终整仓结果以完整canonical报告为准。
