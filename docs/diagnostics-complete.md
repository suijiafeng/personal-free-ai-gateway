# 诊断、筛选与脱敏导出

本页描述完整工程版本的私有 API/CLI。没有新增聊天平台或路由编辑服务。所有读取仍需管理身份；消费密钥不能查这些数据，刷新不会调用模型。

## 能看到什么

- `/gateway/status`：配置版本、数据库状态、排空与活动请求数。`retention_ready=false` 表示后台清理失败，此时 `ready=false`，消费请求失败关闭；不能把“数据库能连接”当成服务可用。
- `/gateway/resources`：实际部署、供应商、免费资格复核日期、池的范围/维度/来源/时效、冷却/耗尽与下次可尝试时间。未知不等于满额，观察值不能相加为“总余额”。
- `/gateway/config`：只读顺位、适配路径、能力、资格依据、复核时间及当前摘要。不返回供应商密钥、环境变量引用或内部 API 地址；预览不是逐请求执行保证。
- `/gateway/summary?days=1`：查询 1 至已配置保留天数。请求数与上游尝试数分别计数；完整完成率和备用率都有请求分母，无记录时为 null，拒绝/截断/中断不计完整完成。没有供应商账单时 `billing_zero_confirmed=null`。
- `/gateway/traces`：按事件 ID 倒序分页的脱敏元数据，可还原每个请求的候选判断、所有实际尝试和最终结果。

## 历史时间线

`candidate_decision` 在请求资格检查及原生候选过滤时记录：候选顺位、实际模型、池、是否排除和原因。若原生层只告知某候选不在健康集合内，只记 `native_candidate_unavailable`，不会猜测它的内部原因。重复的同阶段判断不反复写入。

`attempt_started`/`attempt_finished` 记录实际目标、尝试序号、重试来源、开始/结束 UTC 时间、耗时、上游 HTTP 状态、冷却秒数/来源及池范围。`request_finished` 记录整个请求耗时、终态和错误码。候选判断不是额外生成尝试。

流的 `first_event_ms` 从入口到首次非空响应 body 成功交给 ASGI send 计时；`attempt_first_event_ms` 从本次实际上游尝试开始计时。这不证明远端应用已读取字节。非流式或尚未交付事件时为 null。

`usage_source=upstream_observed` 仅在源边界取得完整合法的三项用量时使用；估算或缺失不算实测。SDK 路径的流式实测用量可进入最终私有诊断，公共流仍不承诺 usage 块，详情见 [SDK 适配决策](adr-sdk-stream-bridge.md)。不能用最终缺失用量推断“上游没有消耗”。

## 查询规则

`GET /gateway/traces` 接受以下字段，每个只能出现一次：

- `limit`：1–100，默认 100
- `before_id`：上一页的 `next_cursor`；有新事件写入时仍不会重复已读事件
- `request_id`：UUID；`key_id`：日志中的 16 位十六进制不可逆指纹；`alias`：`general-free`
- `since`、`until`：必须有时区的 ISO 时间，开始包含、结束不包含
- `final_status`：complete / truncated / refused / failed / stream_interrupted / cancelled
- `fallback`：true / false
- `error_code`：例如 rate_limited

请求 ID、身份、别名和时间范围筛选事件；终态/备用/错误筛选符合条件请求的全部时间线。因此一次先限流、再成功的请求可同时匹配 `error_code=rate_limited&final_status=complete&fallback=true`，不会只返回那一个错误事件。返回包含 `schema_version`、筛选范围、排序和下一页游标。

管理员不能通过查询传入 SQL 标识、任意排序或无上限导出。未知/重复字段、无时区时间、非法 UUID/游标返回 400。生产表只使用参数化查询。

## 导出

先创建你拥有的 0700 私有目录。使用已存在的私有环境文件，明确管理端口；只读计划不读取密钥：

```sh
python3 ops/diagnostic_export.py --env-file .env.mock \
  --output "$HOME/.gateway-private/request-diagnostics.json" \
  --request-id 00000000-0000-0000-0000-000000000001
```

加 `--execute` 才读取指定管理员身份、分页查询和写入文件。可组合上述筛选字段，默认最多 10000 事件；超出明确失败，不静默截断。配置切换或游标异常也会停止，不写成完整导出。

只连明确选定的 loopback 管理入口，不继承 shell 代理，不跟随 HTTP 跳转；凭据只取指定的 0400/0600 文件。输出 0600、不覆盖既有文件、不跟随符号链接；不输出提示词、回答、refusal 原文、供应商密钥或管理员密钥。导出仍有使用时间、模型、调用身份指纹等私人元数据，应自行按任务需要删除，不能公开上传。

## 保留范围

- 网关逐事件元数据：按配置最多 7 天，启动时和每小时清理。后台清理失败触发明确告警和拒绝新消费请求。
- 原生每日调用/Token 聚合：清理锁定版本的明确 daily 表清单；仅保留当天及之前 6 个 UTC 日期，避免日级字段额外保留第 8 天。关闭非必要原生 spend updates 和 audit before/after 记录。
- 原生调用密钥、撤销记录、迁移历史：属于鉴权/恢复状态，不能按请求日志 TTL 删除。撤销账本需独立保护，见运维手册。
- 备份：受自己的生命周期和恢复需要约束，按运维工具的显式私有备份清理流程处理；在线日志清理不会穿透旧数据库 dump。
- Docker/Nginx 运行日志：Nginx access log 默认关闭；固定无正文/身份的系统运行类别仅做容量轮转，不冒充调用明细的按天 TTL。源代码测试不能证明 Mac 的日志驱动与磁盘策略已生效。

## 验证边界

实际原生 Proxy/PostgreSQL/TCP 用例验证候选/尝试时间线、429 备用、带筛选分页、流首事件时间和消费身份拒绝访问；临时数据库验证 daily 表 TTL 不删除鉴权撤销历史。ASGI 及真实 TCP 慢读用例验证总时限后能释放本地并发槽，不保证供应商同时停止计量。部署后的 Nginx、真实消费应用及其重试策略仍需现场验证。
