# 额度观察、来源与重置边界

本实现对应原始规格 §10–12、AT-09、AT-13、AT-14 的薄扩展。`gateway/quota.py` 只解析已审核语义的响应头、保存可序列化观察模型并生成当前视图；没有供应商 SDK、网络调用、SSE 解析、余额预扣或第二套路由器。原生 LiteLLM Router 和 PostgreSQL 仍承担原有职责。

## 数据契约

每个观察严格绑定一个已配置池。不得跨池、跨组织/项目、跨模型范围或跨维度累计余额。

| 字段 | 含义 |
| --- | --- |
| `pool_id`, `provider` | 稳定池 ID 与供应商语义 |
| `account_scope`, `model_scope`, `scope_reference` | 经配置审核的账户/组织/项目、适用模型与共享范围证据；不从 Key 数量推断容量 |
| `dimension`, `window`, `reset_timezone` | requests/tokens、minute/day/unknown 与独立重置时区 |
| `limit`, `remaining` | 可空非负整数；缺失不等于 0，也不等于满额 |
| `source`, `reference` | `upstream_observed` / `local_estimate` / `unknown` / `expired` 与证据或估算方法引用 |
| `observed_at`, `expires_at` | UTC 观察时间与有效期；展示读取不刷新观察时间 |
| `reset_at`, `reset_source`, `reset_reference` | 有来源的窗口重置时间；UTC 储存，时区规则单独保留 |
| `known_exhausted`, `next_probe_at` | 从可信观察派生；禁止信任恢复数据中单独写入的状态标志 |

`known_exhausted` 仅在上游明确返回可信 `remaining=0` 时成立；知道余额为零不等于知道何时恢复。没有可信重置证据时 `reset_at` 和 `next_probe_at` 保持为空。本地估算为 0、HTTP 429、Retry-After、用量、失败、超时、余额缺失均不能证明额度耗尽。

观察时间、过期时间和重置时间均使用带时区的 UTC。拒绝无时区时间，不使用服务器本地时区。计数不接受布尔值、浮点、负数、指数、逗号分隔、超长数值或大于有符号 64 位上限的值。

## 公开函数与集成

```python
observe_headers(pool, headers, now, provider=None, *, mapping=None,
                ttl=timedelta(minutes=5)) -> QuotaObservation | None
unknown_observation(pool, now) -> QuotaObservation
snapshot(pool, observation_or_dict_or_none, now) -> QuotaObservation
present_observation(observation_or_dict, now) -> dict
observe_values(pool, now, *, limit=None, remaining=None, reference,
               source="upstream_observed", reset_at=None,
               reset_source="upstream_observed", reset_reference=None,
               ttl=timedelta(minutes=5)) -> QuotaObservation
observe_local_estimate(pool, now, *, limit=None, remaining=None,
                       reference, ttl=timedelta(minutes=5)) -> QuotaObservation
next_local_midnight(tz, now) -> datetime
```

- `observe_headers` 返回 `None` 表示没有新可靠额度字段，调用方不要用空数据覆盖已有观察。只有 limit 或 reset 的部分观察可以存在，缺失 remaining 仍为空。
- `.to_dict()` 返回 JSON 友好对象；模型冻结且不保留原始 headers。`snapshot` 将持久数据重新绑定当前池元数据；缺失、损坏、旧格式、未来观察时间或配置范围变化都降为 unknown。
- `present_observation` 只投影有效性，不更新持久记录、不调用上游、不重置额度。使用恢复数据时优先 `snapshot`，因它另外验证当前配置范围。
- 可信零余额且有已知重置的有效期保持到重置时间；可信零余额但重置未知时使用短 TTL，状态层可依据本地策略在此期间暂停该池，到期仅允许一个真实请求探测。该本地可探测时间不是供应商重置或恢复承诺，不能写入 `reset_at`/`next_probe_at`。正余额、部分观察和估算也使用短 TTL；若已知窗口先结束，使用较早时间。支持的 TTL 为大于 0 且不超过 1 天，未来重置解析上限为 2 天，避免损坏数据制造无限禁用。
- 有效期或窗口到期后来源显示 expired，limit/remaining 清空，取消 known_exhausted 和该观察的 next_probe_at。保留历史观察时间与重置来源供诊断。它只意味着允许重新受控尝试，不意味着供应商保证恢复，也不生成剩余额度。
- `observe_values` 是未来已验证额度接口或测试的显式入口；调用者负责证明值的来源，不允许把本地累计用量调用该函数标为上游余额。`observe_local_estimate` 需要明确方法/版本引用，且永远不会产生耗尽限制。
- 可用性冷却、人工停用与本地探测锁属于现有状态层，不能仅由额度观察覆盖。状态层还须保留较新的共享池冷却/耗尽，避免较早在途请求的成功结果将其抹掉。

## 已核对的供应商语义

核查日期：2026-10-06。只阅读公开官方文档，没有登录真实账户或推理调用，也不据此宣布账户免费资格、具体限额或实际共享模型范围已验证。

### Groq

[官方 rate limits 文档](https://console.groq.com/docs/rate-limits) 将额度描述为组织范围，并明确以下头的窗口：

| 允许的响应头 | 维度 / 窗口 |
| --- | --- |
| `x-ratelimit-limit-requests`, `x-ratelimit-remaining-requests`, `x-ratelimit-reset-requests` | 请求 / 日（RPD） |
| `x-ratelimit-limit-tokens`, `x-ratelimit-remaining-tokens`, `x-ratelimit-reset-tokens` | Token / 分钟（TPM） |

重置头采用 `2m59.56s`、`7.66s` 等时长格式。解析支持有序 d/h/m/s/ms 单位和有限精度，不把无单位数字猜成时间戳。`retry-after` 是重试等待反馈，不是日余额或某一个计量维度重置的证明。

Groq 还可能有模型特定限制，以及额外的输入/输出 Token 限制；[官方 Projects 文档](https://console.groq.com/docs/projects) 说明项目可以设更严格限制。当前六头映射不代表收集了账户所有约束，也不证明不同模型共用一个计数器。未匹配当前池 dimension/window 的头不使用。

LiteLLM 1.104.0 的原生适配器是否保留这些头需要分路径验证。对本地模拟响应的验证显示 Groq 非流式路径在可用扩展点前丢失 headers，因而该路径保持 unknown；流式路径可从保留的官方响应元数据读取。新推荐的 SDK 窄适配从官方 raw response 边界取得非流式/流式头，再执行相同白名单与池映射；真实 Groq 账户仍未验收。旧 native 路径的限制不再笼统套用于 SDK 路径。

### Gemini

[官方 rate limits 文档](https://ai.google.dev/gemini-api/docs/rate-limits) 说明限制以项目为单位，RPD 在 Pacific 午夜重置，不是每个 API Key 独立分配；实际限制也取决于模型和账户层级。

没有给 Gemini 猜测一套通用 quota response headers。只有明确配置 `provider=gemini`、`dimension=requests`、`window=day`、`reset_timezone=America/Los_Angeles` 时，已验证的显式额度观察才能关联到上述日重置规则。规则本身不提供余额。

`next_local_midnight` 根据下一个本地日历日期计算午夜，再转为 UTC，因此春季 DST 切换日可为 23 小时，秋季为 25 小时。不能简单加 86400 秒，也不能把 Pacific 永久固定为 UTC-8。其他地区遇到不存在或有歧义的午夜时拒绝猜测。

## 模拟数据与白名单

`MOCK_REQUESTS_MINUTE_MAPPING` 明确标记为 synthetic，只匹配 `provider=mock` 且为 requests/minute 的池。它使用同一组六个白名单中的请求头，不产生真实账户证据。生产选择不能默默套用模拟映射。

`HeaderMapping` 可明确选择时长、秒数、Unix 秒或 RFC3339 重置格式，禁止自动猜测。所有字段名称仍限制为对应维度的六个已知 quota 头；不能映射 Authorization、Cookie、Retry-After 或任意响应头。重复/冲突的同名字段不可信。未知字段和值不记录，也不在异常中回显。

## 验证范围

```sh
.venv/bin/python -m pytest -q tests/test_quota.py
```

纯函数测试覆盖多窗口独立性、缺失/非法/冲突计数、时长与显式格式、过期而不满额、UTC、DST、损坏恢复、池范围变化、本地估算与敏感原始头排除。这些测试不代替 PostgreSQL 持久化、并发受控探测、原生 SDK 元数据可达性或真实供应商账户验收；对应集成结果应查总测试报告。
