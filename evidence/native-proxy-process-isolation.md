# 原生 Proxy 进程隔离回归

核查日期：2026-10-06。版本：未修改的 LiteLLM 1.104.0。仅使用合成凭据、loopback 模拟上游及新建的临时 PostgreSQL。

## 最小复现与精确原因

同一个 pytest 解释器依次启动 native 适配器 Proxy、执行基本生成、关闭；再启动 SDK 适配器 Proxy、执行相同基本生成。结果：第一项通过，第二项在任何上游调用前返回 503。这个只有两项测试的复现排除了整套负载和流式超时作为原因。

只读诊断发现：

- 第二个 Router 中确实是正确的 gateway_openai_sdk/openai/mock-* 模型。
- Guard 引用的是新 app 的当前 state 对象，持久化状态健康。
- litellm.callbacks 中出现了两次同一个 GatewayGuard 对象。
- trace 显示一次 attempt 预留后又执行重复筛选，把刚被自己占用的未知额度池判为不可用。实际上游调用计数仍为空。

原生[callback 初始化](https://github.com/BerriAI/litellm/blob/v1.104.0/litellm/proxy/common_utils/callback_utils.py)在加载配置时扩展已有全局列表；原生[关闭清理](https://github.com/BerriAI/litellm/blob/v1.104.0/litellm/proxy/proxy_server.py)仅清理部分全局变量，并未重建全部对象或清空该 callback 列表。第二次 lifespan 因而重复登记策略。脱敏观测与来源摘要保存在 native-proxy-process-isolation.json。

不能通过删除安全检查、临时重置原生私有状态，或者忽略失败来“解决”此问题。

## 处理方式

- gateway.bootstrap.build_app 限制每个进程只能成功构建一次原生 app；第二次明确要求启动新进程。失败的构建不计为成功。未引入 vendor reload/reset。
- 部署、发布和重启本来就使用新进程；仍是单实例架构，不支持进程内反复构建不同配置。
- tests/run_postgres_suite.py 的主阶段只排除 SDK Proxy 模块，第二个全新 pytest 进程再原样执行此模块，两者连接同一个临时数据库。
- 任一阶段失败仍执行另一个阶段。保留各自原始 JUnit，合并报告保留全部案例和失败；丢失或损坏的报告明确计为错误，不使用旧报告代替。只有两阶段都通过才开始恢复演练。

六项定向回归验证一次构建、失败构建不占用实例、精确阶段选择、失败保留、失败后继续另一阶段，以及缺失/旧/损坏 JUnit 检测。定向结果：6 passed。最终端到端结论以新的完整 canonical run 为准。
