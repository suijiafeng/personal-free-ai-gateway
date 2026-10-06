# 架构：自用基础 AI 应用的统一后端

目标是一个固定接口、受限 key、明确免费白名单、少量有界备用和必要诊断。保留原生 LiteLLM 1.104.0 + PostgreSQL，不引入多租户、计费、复杂后台、Redis 或第二套路由。

## 运行链路

受限消费身份 → loopback Nginx 消费入口 → ASGI 安全边界 → 原生 Proxy 鉴权 → 官方逐尝试策略 hooks → 原生 Router 按顺位/有限备用 → 明确适配路径 → 批准上游。

推荐路径是官方 `CustomLLM` 的窄文本插件，使用已锁定官方 OpenAI SDK；SDK负责 HTTP/JSON/SSE 解析，LiteLLM负责路由和公共序列化。它解决原生独立 refusal 丢失、用量来源不可验证的已复现缺口；没有 vendor fork、私有 chunk-buffer 读取或自写 SSE parser。详见 [ADR](adr-sdk-stream-bridge.md)。旧原生流路径只留在测试中，生产 validator 拒绝它声明 streaming。

每个工作进程只构造一次原生应用。LiteLLM会追加进程级callbacks，反复在同一Python进程创建不同配置会重复注册安全hook；因此bootstrap拒绝二次构造，配置发布和验收适配路径都使用新进程，不重载/私改vendor全局变量。

独立管理入口只允许原生 key API、脱敏资源/追踪/配置和排空。模型与顺位只有版本化 policy 一个权威源；生成原生 YAML 只是派生文件。原生数据库模型/设置覆盖发生时拒绝运行，不维护第二份可编辑控制面。

## 组件

| 组件 | 职责 |
| --- | --- |
| config/contract | 严格配置图、资格/时效、目标、输入白名单与候选能力交集 |
| ingress | 精确端点/方法、身份隔离、体积/并发/全程时限、头部与错误脱敏；不解析SSE |
| 原生 Proxy/Router | PostgreSQL virtual key、原生API、顺位与有界备用 |
| hooks | 过滤、逐次检查实际目标/凭据/免费/权限/隐私、尝试预算、typed终态与安全审计 |
| stream_bridge | 官方SDK窄契约映射、拒绝/空事件保留、原始用量与响应头来源；不改路由决策 |
| state/quota | gateway_ext独立元数据、范围/来源/时效、冷却/耗尽、单worker探测；不是账单或共享账本 |
| diagnostics/trace_query | 只读状态、分母清楚的汇总、历史决策/尝试、严格筛选分页 |
| ops | 目标隔离、密钥/撤销账本、排空/备份/发布/恢复/保留清理；默认计划 |
| 官方SDK示例 | 首个基础消费应用，终态/错误/取消及禁重试已过完整合成链路 |

## 不可越过的安全顺序

消费白名单与体积校验 → 原生身份 → 配置资格与授权交集 → 原生候选过滤/人工顺位 → 每次调用前重新核对精确模型/地址/凭据/隐私/预算 → 获取明确池占用并记 attempt_started → 实际上游。

候选资格变化、必要数据库/策略/审计不可用、清理任务失效或运行角色权限过高都拒绝新请求。原生吞掉 callback 异常时仍由入口/下一尝试的安全标志阻断。不能用HTTP200代表完整、用429代表精确耗尽、用失联代表没消耗。

任意流事件交付后禁止备用；发生故障只终止并保留未完成语义。SDK bridge只暂存终态标志以等待尾部传输完成，不缓冲整段正文。慢读/挂起close/审计清理各有有界最佳努力，不能永久占槽。生成停止与计量停止不作外部保证。

## 数据与角色

- policy：供应商、模型、资格、顺位、能力、凭据引用和池元数据；不放密钥值
- 原生 PostgreSQL 表：调用key/撤销/原生必要状态，不自建另一套鉴权
- gateway_ext：脱敏request/attempt/candidate事件、池冷却/禁用/耗尽、有来源观察；没有prompt/answer
- 进程内：单worker的池owner/probe锁，不宣传跨实例配额预扣
- 独立私有撤销账本：只存native key hash与不可省略的操作历史/checkpoint，数据库旧备份不能覆盖它
- runtime数据库身份：只DML，启动/就绪实际检查危险权限；migration/backup/bootstrap独立。初始化DDL移至维护步骤，见 [数据库角色](database-roles.md)

元数据按最多7天清理；日聚合按7个UTC日期。身份/撤销历史和备份有各自生命周期，不盲目按调用日志TTL删除。Nginx默认不另存逐请求access log，系统固定类别日志容量轮转不冒充7天调用元数据清理。详见 [诊断](diagnostics-complete.md) 与运维手册。

## 部署

gateway、PostgreSQL、mock上游在internal网络，没有直接宿主端口。Nginx把消费/管理端口分别绑定127.0.0.1。模拟默认选 `policy.sdk.mock.yaml`，真实policy全停用。

真实启用使用显式的 reviewed egress overlay：维护中的Squid仅允许审核文件中的精确官方主机/443 CONNECT并阻断私网/特殊地址；只有出口侧车连接外网。Provider凭据由独立只读secret文件注入，不能靠拆internal网络或继承任意shell凭据凑通。声明和独立组件测试不等于Mac容器网络验收，需运行容器内probe。

基础镜像按不可变索引/平台摘要固定；Python artifact hashes固定。依赖、许可和公告的实际范围见 [供应链](../evidence/supply-chain-complete.md)，不以“0条已知Python漏洞”替代OS镜像安全结论。

## 六模块的可用入口

| 模块 | 首版交付 |
| --- | --- |
| UI-01 总览 | status、1–7日summary，明确完整率/备用率分母及清理/DB失败 |
| UI-02 资源 | resources、资格/池范围/来源/时效，不造余额 |
| UI-03 路由 | 只读config/资源预览、validator、维护发布/回滚CLI |
| UI-04 调用 | 候选历史/全部尝试/终态/时延，组合筛选、游标、脱敏导出 |
| UI-05 接入 | 本地端口/协议、受限key、轮换撤销、版本/备份记录 |
| UI-06 验证 | SDK示例、TCP故障工具、端到端回归，不做完整聊天产品 |

无需另建六个网页。以后只有实际操作痛点才扩界面、工具调用或多进程；本次不把它们当未完成的首版需求。

## 证据分层

纯函数/stub → 真TCP Router → 实际Proxy+PG → SDK参考应用 → 原生进程恢复/DB角色 → 独立出口组件 → Mac/Compose/Nginx现场 → 真实账户/模型。每层各自给证据，不能互相替代。详细状态以最终 [验收矩阵](../evidence/acceptance-matrix.md) 为准。
