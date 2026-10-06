# 基础 AI 应用接入协议

本项目面向个人的小型 AI 应用。附带 `examples/client.py` 是已经穿过实际 LiteLLM Proxy、PostgreSQL 和本地模拟上游验证的首个消费应用；你之后的项目复用同一个地址、受限 key 和别名即可。它不是所有聊天软件或 IDE 的兼容承诺。真实 Groq/Gemini 模型和账户仍须单独审核。

## 请求

只公开两个端点：`POST /v1/chat/completions` 和 `GET /v1/models`。管理主密钥不能用于生成。消费 key 绑定 `general-free`、明确 deployment IDs、非敏感内容范围和到期时间；未知别名返回 404，不走默认模型。

允许的生成字段：

| 字段 | 精确约束 |
| --- | --- |
| model | 必填，general-free |
| messages | 非空有序数组，每条只有 role/content |
| role | system、user、assistant；所有合格候选都须支持 |
| content | 纯文本字符串 |
| stream | 布尔值，默认 false |
| max_completion_tokens | 正整数 1–4096，默认 1024，另受候选限制 |
| n | 只接受整数 1 |

历史原样保序；不保存隐藏会话、不摘要、不裁剪。请求体最多 1 MiB；深度畸形 JSON、重复键、错误类型、非 JSON 请求返回 400。输入字符界限是保守保护，不是精确 Token 上下文计数。

其他字段全部拒绝，包括 max_tokens、temperature、top_p、stream_options、tools、response_format、多模态、Responses、metadata、任意上游 URL/凭据/备用链和 drop_params。SDK 适配器内部把公开输出上限精确映射为供应商兼容端点的 max_tokens，并再次核对值，没有扩大消费端契约。

## 响应与终态

沿用标准 Chat Completions 对象，不另套 data/result。`model` 可以是实际模型；诊断使用 `X-Request-ID`、`X-Config-Revision` 和非流式 `X-Actual-Deployment`。非流式 `X-Usage-Source` 指示已认证来源。

| 结果 | 应用处理 |
| --- | --- |
| stop 且无 refusal | complete |
| refusal 或 content_filter | refused，不自动切换绕过 |
| length | truncated，不算完整完成 |
| 无终态关闭流、流中异常 | interrupted，保留已收到的文本 |
| 首事件前请求错误 | error |
| 用户取消 | cancelled，不自动生成新请求 |

推荐 `adapter=openai_sdk` 的窄文本路径：官方 SDK 负责 HTTP/JSON/SSE，LiteLLM 负责原生路由及公共序列化。独立 refusal-only、文字后的拒绝、role/empty、合法空回答都保留；只把已经收到并经 SDK 完整读取的明确终态交给公共流。终态后的传输异常不会伪装 stop/[DONE]。

旧 `adapter=native` 仍用于非流式和缺陷回归。它会丢失独立 refusal 分片，所以生产配置明确禁止它声明流式能力。不能通过只修改 YAML 的 streaming=true 绕过这一限制。

任意角色、空块或正文事件交付后都禁止透明备用；首事件前可在同一预算内尝试剩余合格目标。全程最多两次实际生成，Router 与官方 SDK 均关闭自动重试。不要在应用里再叠加不受控重试。

公共 SSE 仍是标准 chunks 和正常完成时的 `[DONE]`，没有自定义事件协议。HTTP 200 不能证明回答完整，应用要检查终态。公开 stream_options 尚未开放，公共流的 usage 保持未知；SDK 上游提供的完整合法数值可进入最终私有诊断。未提供用量不是零，不代表上游没有消耗。

## 错误与时限

| 情况 | HTTP / code |
| --- | --- |
| 格式/能力不支持 | 400 / invalid_request 或 unsupported_capability |
| 无效/无权消费身份 | 401/403 / authentication_error 或 access_denied |
| 未知别名 | 404 / model_not_found |
| 本地并发或原生限流 | 429 / rate_limited |
| 没有合格目标或必要状态不可用 | 503 / no_eligible_free_model，或明确状态故障码 |
| 上游异常/凭据异常 | 502 / upstream_error 或 upstream_auth_error |
| 总请求时限 | 504 / deadline_exceeded |

对外错误去掉上游正文、内部地址、密钥和提示词。429 仅在当前失败尝试有经校验的未来等待时间时带数字 Retry-After，不直接透传原始头；没有可信值就省略。备用成功不会把前一目标等待误加到成功结果。

单 worker、并发 2；总时限最多 90 秒，包含上传、等待和流过程；首事件最多 15 秒、流空闲最多 20 秒。超时停止生成/读取；对慢读连接，错误或 EOF 发送另有 250 ms 最佳努力关闭预算，审计清理最多 5 秒，原生请求槽清理另有独立 1 秒预算，不能永久占槽。原生槽通过锁定版本自身的幂等生命周期方法释放，细节见 [请求槽清理](../evidence/native-request-slot-cleanup.md)。取消/关闭是尽力而为，不能保证供应商同时停止执行或计量。

## 运行示例

在按 Mac 手册启动模拟网关并取得受限调用 key 后，客户端只需独立的 16 包锁文件，不必在应用环境安装整套服务端：

```sh
python3 -m venv .client-venv
.client-venv/bin/python -m pip install --require-hashes -r examples/requirements.lock
export GATEWAY_BASE_URL=http://127.0.0.1:4100/v1
.client-venv/bin/python examples/client.py --key-file secrets/local-app.json --no-stream --prompt '请用一句话解释什么是 API。'
.client-venv/bin/python examples/client.py --key-file secrets/local-app.json --prompt '请用一句话解释什么是 API。'
```

示例默认超时 30 秒、max_retries=0，不创建密钥、不自行换模型。取消会关闭 SDK 流并保留已收到内容。stdout 是回答，stderr 是无正文的状态元数据；退出码 0=完整，1=请求错误，2=拒绝/截断/中断/取消。命令行 prompt 可能进入 shell 历史和进程列表，仅用非敏感内容。

源码集成使用 `make_client()`/`consume()`；完整必要历史由应用传入。正常只需设置 base_url、受限 key、general-free，不需要理解供应商格式。

## 验证入口

- `tests/test_mock_upstream.py`：官方 SDK 直接访问模拟器
- `tests/contract/test_sdk_bridge.py`：原生 Router + 官方 SDK 窄适配 + TCP 故障
- `tests/integration/test_sdk_proxy_postgres.py`：实际 Proxy/受限 key/PG/公共线级流
- `tests/integration/test_sample_consumer.py`：附带应用直连上述完整链路，包含错误和取消
- `tests/test_downstream_backpressure.py`：ASGI 与真实 TCP 慢读释放

这些不消耗真实免费额度，也不能证明你的供应商账户允许此调用。镜像/Mac/入口出口现场测试另列于 [验收矩阵](../evidence/acceptance-matrix.md)。
