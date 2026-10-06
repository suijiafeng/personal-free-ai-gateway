# 当前能力矩阵

面向个人基础 AI 应用的完整工程候选，日期 2026-10-06。工程验证使用官方 SDK、原生 LiteLLM Proxy、实际 PostgreSQL 和合成 TCP 上游；不把模拟结果当真实账户或 Mac 部署通过。

## 对外契约

| 项目 | 当前边界 |
| --- | --- |
| 端点 | POST /v1/chat/completions、GET /v1/models |
| 模型 | 受限身份获准的 general-free；未知别名 404 |
| 消息 | 有序纯文本 system/user/assistant，完整必要历史由应用传入 |
| 字段 | model、messages、stream、max_completion_tokens、n=1 |
| 输出上限 | 默认 1024，最大 4096，另受每候选能力限制 |
| 输入 | 1 MiB 请求体及各候选保守字符上限；不声称精确 Token 计算 |
| 其他能力 | max_tokens/采样参数/stream_options/tools/结构化输出/多模态/Responses 全部明确拒绝 |
| 安全 | 不接受客户端地址、凭据、备用链、metadata 或参数降级覆盖；管理员不能生成 |

## 路径资格

| 配置/适配 | 文本 | 流式 | 真实账户状态 |
| --- | --- | --- | --- |
| policy.sdk.mock.yaml / openai_sdk | 原生完整链路通过 | 正常/拒绝/空流/截断/故障/取消均有专项证据 | 合成，仅本地/容器内模拟；推荐起步 |
| policy.mock.yaml / native | 原生完整链路通过 | 保留原生拒绝丢失的已知缺陷复现 | 仅回归实验，不作推荐流式演示 |
| 生产 Groq / openai_sdk | 工程路径可配 | 按具体模型资格开放 | enabled=false、streaming=false、unknown |
| 生产 Gemini 官方兼容入口 / openai_sdk | 工程路径可配 | 按具体模型资格开放 | enabled=false、streaming=false、unknown |
| 生产 native + streaming | 不适用 | schema 明确禁止 | 不能通过配置误启用已知不安全流路径 |

生产只有精确已审核的 Groq/Gemini HTTPS 主机/兼容路径，不能使用任意聚合地址。填写资格字段不是事实核查；真实账户还需免费硬边界、隐私、精确模型能力和额度作用域证据。

## 行为与诊断

| 能力 | 当前实现 |
| --- | --- |
| 顺位 | 免费/权限/隐私/能力/池先筛选，原生 order 再选择；每次实际调用前重新核对目标 |
| 备用 | 最多两个独立批准候选、两次实际生成；SDK/Router 禁重试；拒绝与 401/403 不扩散 |
| 流提交 | 角色/空块/正文任意事件之后不透明换模型、不拼接；缺终态或传输失败保持未完成 |
| 拒绝 | SDK 路径保留独立 refusal-only、文字后 refusal；不把拒绝或截断算完整完成 |
| 用量 | 原始/官方 SDK 完整合法三项才算 upstream_observed；缺失/部分/非法为 null/unknown；公共流 usage 不开放 |
| 超时/取消 | 上传、首事件、流空闲、总时限；慢读最佳努力限时关闭，必要审计有界清理，槽总会释放；不保证供应商停止计量 |
| 状态 | 多维池、真实零耗尽、短期冷却、来源/时效、范围指纹、单探测、重启恢复；不会补满额度 |
| 历史诊断 | 候选排除、实际尝试、耗时、首事件、重试/冷却、HTTP状态、最终结果、key指纹、配置摘要 |
| 查询/导出 | 请求/时间/身份/终态/错误/备用筛选、稳定游标、0600脱敏导出，来源和分母明确 |
| 保留 | 逐事件最多7天，原生daily表最多7个UTC日期，清理失败就绪为false；鉴权撤销单独保留 |
| 密钥与恢复 | 原生受限key、轮换/撤销；独立撤销账本；旧备份恢复保持排空并核对/重放，防备份后撤销复活 |
| 配置与备份 | 文件唯一权威、验证/排空/快照/发布/回退；镜像/migration/checkpoint护栏；失败pg_restore事务回滚有真库证据 |
| 数据库权限 | 四独立角色；runtime只DML，backup只读，migrate非超级用户；真实SCRAM/native流程已验证 |
| 出口与供应链 | 不可变基础镜像摘要、hash锁依赖、官方Squid固定源及精确CONNECT ACL；完整Mac容器路径待现场实测 |

## 管理方式

六模块分别通过 status/summary、resources、只读 config/发布工具、traces/导出、原生 key API/CLI、官方 SDK 示例交付。原规格允许这些操作路径；不要求新的六页网站，不做多租户、充值、收费兜底或高可用平台。

边界：单实例、单 worker、并发2、总尝试2、总时限90秒、首事件15秒、流空闲20秒。应用自己显式发起的第二个请求是新请求；网关不保证外部工具恰好执行一次。

最终源版本的实际检查见 [测试记录](../evidence/test-results.md) 和 [逐项验收](../evidence/acceptance-matrix.md)。Mac/Docker/Nginx网络全链路、真实账户与具体模型仍有明确现场门槛；用户日后的小应用不是额外预先开发要求。
