# 免费供应商接入与发布门槛

默认 `config/policy.yaml` 没有任何启用的真实供应商。Groq、Gemini 只是准备文档中的候选，不能从品牌、模型名包含 free、公开价目表或模拟测试推断当前账户可免费使用。

## 1. 先确认账户与权限，不先发送提示词

每条实际 deployment 至少记录以下内容，不填写秘密值：

| 项目 | 必须留存的证据 |
| --- | --- |
| Provider/实际模型/官方 endpoint | 精确模型 ID、受控 HTTPS 主机、版本与官方来源 |
| 账户范围 | 非秘密账户/项目/组织标识；权限是否足够 |
| 免费条件 | 当前账户有权使用、推理确实不收费的依据与核查日期 |
| 付费边界 | 账户侧禁止收费或有可信硬限制；不接受自动升级、付费备用或试用到期后收费 |
| 隐私 | 账户、地区、条款版本、保留/训练/人工审阅条件及批准内容范围 |
| 复核 | 复核期限、负责人、来源；过期资格必须禁用 |
| 额度池 | 组织/项目、模型适用范围、RPM/RPD/TPM 等分开、重置时区与来源 |
| 实际能力 | 角色、纯文本、SSE、结束与 usage 行为、输出上限、允许参数 |
| 独立性 | 第二渠道是否真有独立额度和故障来源，不是同池第二把 key |

本项目默认只接受非敏感学习/测试文本。不承诺自动检测公司代码或敏感个人信息；未经审查不能传给免费渠道。

核查入口：[Groq rate limits](https://console.groq.com/docs/rate-limits)、[Gemini rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)、[Gemini 服务条款](https://ai.google.dev/gemini-api/terms)。这些链接仅作为复核入口，不能代替你的当前账户证据。

## 2. 凭据与网络

1. 只有经授权才创建、保存或配置持久供应商凭据。凭据放服务器秘密存储；policy 仅引用环境变量，不把值复制进配置、提交记录或报告。
2. 添加精确官方 host 与模型 allowlist，不允许消费端指定 api_base、api_key、代理 URL、文件路径或备用链。
3. 默认 Compose gateway 位于 internal 网络，没有公共出口。仓库已提供 reviewed egress overlay、固定官方 Squid、精确域名/443 CONNECT ACL和独立 provider secret 文件注入；默认域名清单拒绝所有真实目标。启用前按 controlled-egress.md 核查配置，并在实际容器执行DNS、IPv4/IPv6、重定向、云元数据/私网阻断测试。
4. 不直接把 gateway 加入不受限 external 网络，不拿环境变量 HTTP_PROXY 当作所有 SDK 已受约束的证明。要从实际容器验证指定 SDK 确实走代理、直连被阻止、未批准地址不可达；保存脱敏网络证据。
5. 上游返回 redirect 不代表可以接入新 provider；新增主机必须重新审批与资格核查。

此仓库不擅自修改宿主防火墙、安全策略或用户账户，也不替用户接受供应商条款。需要操作者先明确授权相应步骤。

## 3. 接入顺序

1. 首个消费端已经是附带的官方SDK参考应用，非流式/流式/错误/取消和禁重试均通过实际Proxy/PG/合成上游。你的基础AI项目可以复用这一契约，不必先选某个商业IDE。换用其他SDK或应用时另核对默认字段、结束判断和重试。
2. 在禁用状态下把供应商记录放入配置草稿，填写免费/隐私证据、凭据引用、能力和额度池。validator 不访问账户，不会把字段填写完整误认为事实已经验证。
3. 先跑生产配置 read-only 校验及全套 mock 契约/安全/故障测试。未知免费、付费、隐私不符、能力不符的目标，必须观察到上游调用数为零。
4. 获准后做单一真实目标的非敏感非流式与 SSE 调用。调用会消耗免费额度；记录真实 usage 来源和终态，不把本地估算充作上游实测。
5. 独立核查第二目标；用可控模拟故障验证资格过滤和最多两次尝试，不能用反复真实限流来替代故障模拟。
6. 比较所有候选能力交集。默认拒绝 tools、response_format、多模态、temperature 等未获批准参数；不为使备用成功而丢字段。
7. 维护窗口发布，验证实际 config_revision 与路由顺序。给消费端创建单独受限 native key；不得把管理员主密钥交给应用。
8. 使用已验证参考应用完成真实账户最少联调；账户资格、目标Mac/容器供应链与全部适用现场验收通过后才记录真实渠道可用。失败时停用目标；不偷偷启用付费兜底。

## 4. 每条真实渠道的验证记录模板

以下是人工需填写的记录结构，空值保持未验证，不生成“通过”证据：

```yaml
status: unverified
deployment_id: <stable-non-secret-id>
provider: <provider>
real_model: <exact-model-id>
account_scope: <non-secret-account-project-or-org>
endpoint_host: <approved-official-host>
free_eligibility:
  conclusion: unknown
  evidence_url: null
  account_evidence_reference: null
  reviewed_at_utc: null
  review_due_at_utc: null
  automatic_paid_upgrade_disabled: null
privacy:
  permitted_scope: non_sensitive
  applicable_terms_url: null
  account_and_region_reviewed: false
quota:
  shared_pool_scope: unknown
  dimensions: []
  reset_timezone: null
capability_tests:
  non_streaming: not_run
  sse_normal: not_run
  first_event_failure: not_run
  after_first_event_failure: not_run
  unsupported_parameter_rejected: not_run
  fallback_authorization: not_run
  usage_provenance: not_run
client:
  name_and_version: null
  retries_disabled_and_verified: false
evidence:
  config_revision: null
  trace_ids: []
  upstream_attempts_observed: null
  reviewer: null
```

此模板是核查说明，不是运行 policy schema，不应直接替换 `config/policy.yaml`。按当前严格 schema 填写实际配置，证据链接可以指向受控文档，不必把私有账单/账户信息放公共仓库。

## 5. 停用、复核与回滚

免费条款、账户账单模式、隐私范围或可用模型发生变化时，先停止新请求或停用目标，再复核。资格过期不能因“最近调用成功”而继续视作免费。冷却/余额过期不是免费资格过期，两种状态分开记录。

配置停用通过同样的 validator → mock → drain → 发布 → 实际 revision 验证流程。紧急密钥撤销通过 private native key API，并验证旧 key 实际失败。回滚旧配置也要重新核对其中资格是否仍有效；旧配置曾经通过不等于今天仍允许使用。
