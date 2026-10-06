# 原规格独立完成度审查

审查日期：2026-10-06 UTC。权威需求是 [原始规格](../docs/source-spec.md) 全文，特别是 20 项 M 级 FR、G-01–06 与 AT-01–24。本审查重新读取实现与精确版本的原生代码，不以既有验收矩阵或测试总数代替结论。

## 最终签核

在原规格的个人自用、单实例、受限文本 Chat Completions 范围内，源码和本轮可执行的 Linux/原生 PostgreSQL/合成上游验收可以交付。经逐项复核，未发现尚未关闭的原规格 M 级实现阻塞；这不是“没有任何潜在缺陷”或“已在 Mac 上线”的声明。

最终 canonical runner 已退出 0。独立重新解析两个原始 JUnit 和合并 JUnit：main 748 个 testcase +61 个 subtests，SDK 41 个 testcase +10 个 subtests；合计 789 个独立 testcase、71 个 subtests，failure/error/skip 均为 0。另行 8 项恢复守卫通过；189 项原生 migration 完成。没有把 860 个含子测试的计数称为 860 个独立功能。

签核不仅依据数量：已逐项核查原始 M 需求、原生回调和槽释放的版本绑定签名、已交付流禁止备用、拒绝保留、未知用量、先验授权、私有密钥/日志、单一配置权威、真实数据库故障/事务恢复、旧 key 撤销重放和新用户操作路径。`native-recovery.json` 确认恢复的原 key 在前后都保留 max_parallel_requests=2，连续三次 deadline 后同 key 均能正常调用，既未换 key 也未清计数；`database-recovery.json` 确认失败 pg_restore 的部分 DDL 全部事务回滚，86 张表的恢复内容一致。最终恢复完成并停止、删除本轮临时数据库。

真正仍受外部条件阻塞的原规格门槛只有：
1. 真实 Groq/Gemini 账户、具体模型、凭据归属、免费硬边界、条款/地区隐私及独立额度池核查和最少真实调用。对应 FR-05/06/14/15 和 AT-24 的实际账户证据；未核查渠道继续 disabled/unknown。
2. 目标 Mac 的 Docker/Compose 构建与完整入口/出口链路、跨 UID 私有 secret 读取、TLS/私有网络、实际容器发布/恢复和慢读长连接验收。对应 FR-03/19/20/21 的真实部署组合证据；静态检查、原生进程和独立 Squid 组件测试不能替代它。
3. 最终自建容器工件的完整来源/SBOM/适用签名及扫描；若未来选择跨版本升级，必须先验证明确的镜像/schema 组合。当前代码会拒绝未经证明的跨镜像/跨 schema 自动恢复，没有宣称完成未知版本迁移。

首个个人应用消费端由附带 SDK 示例承担，已穿过真实原生 Proxy/PostgreSQL/合成 TCP 上游验证；不再要求额外商业 IDE 或尚未开发的用户项目。自研六页 UI、Redis/多实例账本、工具/结构化输出等没有被添加成首版条件。

## 结论与证据边界

源码可以完成的工作和真实部署验收必须分开。真实账户、用户 Mac/容器入口没有因为合成测试成功而自动通过。用户于本轮明确用途为个人自用、基本 AI 应用项目，因此所附 SDK 示例可以作为首个消费端实现，不要求商业 IDE 或尚未编写的业务应用先行接入。生产策略仍须保持未核查目标停用。已有完整的 API/CLI 操作路径可以满足六个逻辑模块；原规格明确不要求首版另写六个网页。

本轮审查发现了可实现缺口，已向实现负责人逐项交接。尤其是慢读连接在总时限后仍能占住并发槽，不能归类成“仅缺真实主机”。其他已指出的缺口包括请求时决策诊断、清理失败的就绪状态、原生日统计保留、生产出口/凭据装配和数据库角色分离。下表是需求检查清单；最终执行结果以本次完整回归及各专项报告为准。

## 每一项 M 级需求

| 要求 | 独立检查的实现位置与必要行为 | 剩余验收边界 |
| --- | --- | --- |
| FR-01 统一文本入口与授权模型列表 | `ingress.CONSUMER` 精确放行两个端点；原生 Proxy 鉴权；`async_filter_listed_models` 只保留 general-free；输入/错误契约已明示 | 示例消费端到原生网关链路已通过；目标 Mac/Nginx 入口仍需现场验收 |
| FR-02 别名与实际模型关联 | policy 固定 alias；原生 model_info/部署 ID；响应模型、追踪头和每次尝试关联 | 实际模型身份须与真实账户核对 |
| FR-03 非流式、SSE、取消、ID、终态 | 原生序列化/Router；受限官方 SDK bridge 保留拒绝；typed streaming hooks；总时限、取消和真实 TCP 慢读释放 | 旧 native adapter 的拒绝丢失仍存在，但生产 schema 阻止该流式组合；显式 SDK adapter 已通过拒绝与示例客户端链路验证 |
| FR-04 输入白名单与能力校验 | `contract.validate_request/check_capability`；禁止额外字段/角色/模态/工具；入口和逐尝试都检查，drop_params=false | 真实候选能力需要账户级实测；不能仅凭模型名称填写 |
| FR-05 主备免费、授权、隐私约束 | `_eligible`、过滤 hook 和 pre-attempt hook 重复检查；资格时间/硬性免费条件/身份授权均须满足 | 免费证据与隐私条款必须由真实账户/地区核查；代码不能证明人工填入的事实 |
| FR-06 无收费备用与调用覆盖 | 唯一 general-free；生产只允许已审查直连供应商域名；客户端地址/凭据/路由覆盖字段和头不传入原生层 | 出口网络须实际限制并验收，不把域名配置校验当网络防火墙 |
| FR-07 独立受限密钥、轮换、撤销 | 管理主密钥不能推理；原生虚拟密钥有 alias/部署/privacy/到期限制；key helper 与恢复演练 | 原生进程/真实数据库的轮换、撤销及重启恢复已通过；目标 Mac/Nginx 撤销时点仍需现场验证，创建持续访问按授权执行 |
| FR-08 不可用明确失败；拒绝/截断/中断不伪成功 | 无合格候选时拒绝；非流式保留 refusal/length；角色或正文后失败不备用；不造成功 SSE | 已知旧路径被生产 schema 阻断；SDK 路径已验证独立拒绝、文本后拒绝及终态后异常，真实模型仍须开户后资格测试 |
| FR-09 缺失用量未知，估算标来源 | 原始非流式 JSON 的完整整数 usage 才认证；缺失/非法值为 null/unknown；流式无可靠证据时 unknown | 精确流式 usage 不是必需条件。真实供应商 schema 必须单独认证 |
| FR-10 保留历史，无隐式裁剪 | 有序纯文本 messages 原样转发；输入字符上限只拒绝；主备 TCP 请求比较 | 字符门槛不冒充精确 Token 上下文；消费端须发送必要历史 |
| FR-12 先筛选再人工顺位 | 原生 async_filter_deployments 后执行 order；distinct order=1/2；逐尝试再验证 | 两个真实渠道独立故障/额度来源需要证据 |
| FR-13 有界尝试与全程时限 | Router/SDK retries=0；统一 monotonic 请求预算；max_attempts<=2；已提交 SSE 禁止再尝试；慢上传/持续流/慢读分别覆盖 | 示例及后续应用不能自行叠加重试；无后台竞速或隐式多级备用 |
| FR-14 限流、耗尽、作用范围 | 显式 pool 模型；可信零与 429 分开；同池单探测；未知共享关系不合并 | 真实 pool 共享范围与窗口须账户核查；可信等待只提供可尝试时间 |
| FR-15 观察维度、来源、时效与未知 | `quota.py` 和 resources 逐池展示；requests/day 与 tokens/minute 分开；UTC/重置时区独立 | 不能把公开限额文档或成功调用当实时余额；不能保证到点满额 |
| FR-16 重启保留与抑制探测 | PostgreSQL 持久池/观察；scope fingerprint；缺失/过期 unknown；无周期性生成探测 | 单 worker 是明确范围，多实例共享账本属于后续；实际容器重启仍需验收 |
| FR-17 请求 ID、全部尝试、目标、终态与版本 | 请求/尝试分开；每次实际目标及 revision；本轮补请求时候选决策、时延、重试/冷却诊断 | 不能用当前资源状态替代当时决策；拒绝语义取决于上游信号没有丢失 |
| FR-18 默认不存正文/密钥 | 白名单结构化事件；原生任意日志脱敏；SpendLogs/ErrorLogs 默认关闭；公共错误不透传原文 | 原生日统计、操作日志、备份生命周期需各自明确，不能都声称受 events 的 TTL 覆盖 |
| FR-19 唯一配置权威，发布与回滚真实 | 文件编译单一权威；检测 DB 覆盖；只读管理；维护排空、不可变快照、实际 revision/readiness 后才成功 | Docker 发布事务与原生 A→B→A 是不同证据；失败状态不能写成已发布 |
| FR-20 固定版本、升级前回归、备份 | LiteLLM 精确版本拒绝漂移；Python hash lock；同版本 PG 备份/恢复；受控升级手册 | 镜像/OS/Prisma 完整来源、平台构建、真实升级与失败迁移必须有单独执行证据 |
| FR-21 入口与目标隔离 | ASGI 管理身份检查；Nginx 精确端点；loopback 分端口；容器无直接宿主暴露 | 生产出口的装配与实际网络限制必须同时存在；目标主机 TLS/私有网络不能靠声明证明 |

FR-11/23 为 L 级，不属于首版必须实现；FR-22 为 S 级。原始文档中的 Redis、多实例账本、完整自研 UI、工具/结构化输出、Responses、自动质量评分、收费兜底都不能变相加入首版必做清单。

## 本轮直接发现与修复的慢读缺陷

原实现将主 ASGI app 包在总时限中，但 TimeoutError/Exception 分支随后在超时保护之外再次 await 下游 EOF。若下游停止读取，第二次 send 可以永远阻塞，finally 不执行，并发槽不释放。独立复现：50 ms deadline，在 302 ms 仍 task_done=false、active_requests=1；直到人为解除阻塞才退出。

已在 `gateway/ingress.py` 修复：错误响应与 EOF 使用 250 ms 最佳努力发送预算，必要审计清理最多 5 s，槽释放在无条件 finally。`hooks.py` 的 streaming finally 分别限制审计清理和上游 aclose；关闭未确认只记安全诊断，不谎称上游已停止计量。

`tests/test_downstream_backpressure.py` 的五个独立场景已执行通过：

1. 下游 SSE body send 持续阻塞，总时限后仍能释放槽且不发虚假 DONE。
2. 尚未发头时错误响应本身也被慢读阻塞，不无限占槽。
3. 必要审计存储挂起时限时失败关闭，但本地槽仍释放，不制造已留痕记录。
4. 上游 aclose 挂起，不阻塞本地完成清理。
5. 真实 Uvicorn/TCP，服务端/客户端均使用小缓冲；客户端完全不读取且保持连接，服务端在客户端断开前自行取消发送并释放槽。

前四项是明确 ASGI/typed-hook 故障注入，第五项是真 TCP flow-control。它们不冒充 Nginx、Docker、原生供应商或用户实际应用的全链路测试。正常完成/截断/拒绝和慢上传原有七个回归亦通过。

## 个人应用首消费端已直接联调

依照用户本轮“个人自用，做一些基本 AI 应用项目”的说明，将附带 `examples/client.py` 作为首个消费端，而非要求另选商业 IDE。独立新增 `tests/integration/test_sample_consumer.py` 并用全新临时 PostgreSQL、真实原生 Proxy 进程、真实 TCP 合成上游执行：2 项场景通过，63.33 s（冻结 SDK bridge 后重新执行）；记录在 `sample-consumer-integration-results.xml` 和 `sample-consumer-integration-console.txt`。

- 原生默认 adapter：SDK 示例完成非流式生成，验证有实际响应/请求 ID、仅首候选一次调用。
- 受限 SDK bridge：同一示例完成非流式、SSE、未知别名明确错误且上游调用不增加；用户取消保留部分输出、释放本地/上游连接且不切备用；请求完成记录与实际 revision 一致。
- 客户端 `max_retries=0`；公开 SSE usage 仍为 unknown；并未将合成上游当成真实免费账户。

因此首消费端的“代码与网关联调”不再是待用户选择应用的阻塞。用户将自己日后的项目接入是使用阶段；真实账户启用和目标主机部署仍按各自门槛检查。

## 官方 SDK 薄桥的独立复核

已通读 `gateway/stream_bridge.py` 与 [适配决策](../docs/adr-sdk-stream-bridge.md)。本桥使用官方 OpenAI SDK 解码 HTTP/JSON/SSE，并通过公开 parse(to=...) 返回未强制转换的字典；LiteLLM CustomLLM 做已解码字段映射，原生 Router/Proxy 仍负责调度与对外序列化，没有 vendor patch 或自行解析 SSE。适配代码仍有维护成本，不能宣称不存在新增适配层。

独立检查了：精确请求对象/attempt/deployment/pool/消息/地址/凭据绑定、SDK pin、零重试、禁重定向、参数白名单、独立 refusal 字段、先输出正文但暂存终态至安全 EOF、取消关闭、真实 usage 与归一化估算隔离。审查另发现 SDK 非流式 `audio`、`annotations` 是声明字段，不在 `model_extra` 中，最初会被遗漏；现已明确拒绝非空值并加入真实 TCP 非流式用例。

独立执行 `tests/contract/test_sdk_bridge.py tests/test_sdk_bridge_config.py tests/test_downstream_backpressure.py`：55 项通过、30.47 s（最终未强制转换字典版本），证据为 `independent-sdk-backpressure-results.xml`/`independent-sdk-backpressure-console.txt`。包括拒绝形态、断流位置、未知/实测用量、非法输出、无授权调用以及真实 TCP 慢读。本次有 17 条上游警告（Pydantic ReadOnly 和 Router-only fixture 的未 await 冷却回调），没有声称运行无警告。完整 native Proxy/DB 结果与最终整仓回归另外记录，不将本轮 Router 测试替代它们。

## 必须单独核对的运维与数据范围

- `disable_spend_logs=true` 不等于不保存任何原生调用汇总。既有原生数据库快照实际包含 DailyTeamSpend、DailyUserSpend、DailyGatewayRequests；精确版本原生 SpendLogCleanup 不清理这些 daily 表。本轮 `PostgresState.cleanup` 已加入固定 native daily 表清单的 UTC 日期清理；最终完整 DB 回归已验证受控样例。
- 原生 AuditLog 开关默认受环境/premium 状态影响，本轮已显式固定关闭，避免继承环境开启完整 before/after 对象记录。
- DeletedVerificationToken 是鉴权/撤销历史。不能为了满足七天请求元数据 TTL 而盲删必需恢复证据；需要单独说明用途和访问范围。
- Nginx/Docker 的容量轮转不等于七天时间删除。必要诊断和备份分别记录保留期限；旧备份中的元数据不会因为在线表清理而消失。本轮 Nginx 不再保存额外请求访问日志，新增仅处理显式纳管备份、默认预览的保留工具。
- 原初生产配置把应用、迁移、备份都接在 PostgreSQL bootstrap superuser `gateway`。§12.2 要求独立凭据，本轮已分离 bootstrap、migration、runtime、backup 身份，runtime 不拥有 schema 且拒绝 DDL/角色/迁移记录修改；`database-roles.json` 记录实际 SCRAM、native Proxy、备份恢复检验。测试中 disposable trust 数据库仍不能充作生产模板。
- 内部网络默认无外部出口是安全状态；本轮已增加现成 Squid 的显式 allowlist 出口与 file-backed provider secret overlays，默认拒绝；还需在目标容器验证。不能要求用户临时拆掉隔离来启用渠道。

恢复/发布运维的交叉复核另外发现：最初维护 CLI 只加载 base Compose，重新创建 gateway 会丢掉新生产 egress/provider overlays。现已改为显式选择已审核 stack、核对运行容器的项目/服务/config hash、将有效 stack 指纹纳入备份，禁止 base-only 回退。改变启用 provider 集合在停止服务前明确拒绝，另按已有入口/ACL/secret 的协调维护流程执行，不伪装成通用单文件热发布。

撤销恢复顺序已检查：意图先 fsync → 原生删除/验证 → 独立账本 checkpoint；恢复时仍 drain → 事务 pg_restore → 最小权限重建 → 精确 schema/image/revision 核验 → 重放已记录撤销 → 单独 resume。账本覆盖明确为 managed-helper-only，raw API/UI/其他脚本历史须额外确认。不能把存在账本当成已证明全部历史。上述源代码审查不冒充 Docker CLI 实际运行证据。

## 六模块与真实完成门槛

| 原模块 | 原规格允许的首版完成方式 |
| --- | --- |
| UI-01 总览 | 私有 status/summary、明确统计分母、清理/DB 失败不显示健康或零数据 |
| UI-02 资源池 | 逐池 resources、配置证据、来源/更新时刻/过期/共享范围 |
| UI-03 路由 | 只读 config、实时非推理预览、validator、版本发布及回滚路径 |
| UI-04 调用记录 | 可查全部尝试及请求时决策、时延/原因/终态；脱敏筛选导出 |
| UI-05 接入设置 | 明确端点/能力/版本；原生受限 key API/CLI、轮换撤销、备份测试记录 |
| UI-06 验证工作台 | 官方 SDK 示例与可复现故障工具；取消保留未完成；不使用主密钥生成 |

G-01 以附带的 SDK 参考应用作为首个消费端，直接连原生 Proxy/数据库验证正常、流式、错误和取消；G-02 必须封闭实际主备；G-03 必须保证所有已交付事件后无透明备用且不伪成功；G-04 必须实证时限/尝试/取消/冷却；G-05 必须有真实安全追踪；G-06 必须有精确版本的许可/安全/供应链记录。AT-01–24 应逐项对应这些行为，不能把若干参数化断言相加后宣称全部产品验收通过。

可预先完成源码、隔离合成验证、原生同版本恢复和操作手册。不能代办或虚构的关口是：真实账户免费/隐私证据和模型能力、目标主机连接/网络/TLS/容器验收；流拒绝缺陷已通过明确批准的官方 SDK 薄桥收敛，旧 native 流式组合仍不可启用。对这些关口应列出准确的最后步骤，而非称“全部 V1 已上线”。日后将用户自己的基础 AI 项目接入属于使用阶段，不因尚无特定商业客户端而拖延源码交付。

## 最终独立执行摘要

冻结相关源码后，独立再次运行：SDK/schema/慢读阶段性独立检查 55 项通过（17 条已说明上游警告）；参考应用到真实原生 Proxy/PostgreSQL 2 项通过；运维/恢复保护 150 项及 42 个 subtests 通过；离线供应链完整性与部署静态检查通过。部署检查仍明确 activation_ready=false，没有伪称连接或启动用户 Mac。测试源码指纹与报告路径见 `independent-audit-snapshot.json`。整仓最终回归由主交付报告另行记录，本摘要不替代它。

## 首轮整合失败及明确处置

首轮完整集成并非全绿。原生适配与 SDK 适配的两个 Proxy lifespan 在同一 pytest 解释器连续运行时，第二次基本生成返回 503，上游实际调用数为零。已用两条测试和安全探针收敛，排除了“只因为整仓负载或流超时”的解释。

独立核对安装源码 `litellm/proxy/common_utils/callback_utils.py` 第 398 行，确为对已有 `litellm.callbacks` 执行 `extend(imported_list)`；SHA-256 与 `native-proxy-process-isolation.json` 的 `9deb03292ce724fc9642b23435ce979ef2378a3d8a98f236f583ac30f3581960` 一致。安全探针记录第二个 lifespan 注册了两份同一 Guard：第一次已预留的未知池被第二次 Guard 当成不可用，因此没有实际上游调用。详细复现和边界见 [进程隔离诊断](native-proxy-process-isolation.md)。

修复符合原规格单进程/单配置范围：同一进程只允许一次成功构建原生 app；重新部署使用新进程。canonical runner 把原生和 SDK Proxy 模块放在两个新 pytest 进程，仍使用同一个一次性数据库；未删除测试或弱化断言。独立审查合并逻辑确认：任一 phase 失败都会令整体失败；原始 testcase/failure 保留；缺失/损坏报告新增显式错误；运行前删除旧报告以免误用过期成功记录。

另补总 monotonic 时限优先于原生迟到的 200/503；测试夹具先完成两种模式的正常预热，并发测试等待实际 admission，而非依赖固定短睡眠。后来持续流 fixture 另按下节明确校准；首次失败没有被删除或改写为成功。

### 第二轮整合仍发现原生可选计数器泄漏

分进程后的第二轮 SDK phase 仍有 6 项失败；不能把第一次生命周期修正当成整体已经通过。故障呈现先发生总超时，后续同一 key 的正常调用被 429 阻止。继续诊断确认，所选原生版本的可选每-key `max_parallel_requests` 预留在取消/超时路径没有可靠释放；仅等待、换 key 或清空内部缓存不构成修复。

已重新逐字核对原规格：FR-07（原文第108行）要求独立受限 key、主身份隔离、撤销和轮换，没有要求每-key 并发计数；第12.1节（第397行）要求网关总并发 2、包含流且超额有界拒绝；AT-22/23（第681–682行）要求资源释放和超额控制。因此允许去掉不可靠的原生可选每-key 计数，继续保留已经验证的 Ingress 全局并发 2、全部鉴权/免费/隐私/时限/尝试限制。

若采用移除方案，必须同时封闭管理 API 和 helper 重新引入该选项的路径，明确处置旧/恢复 key 的不受支持设置，且不默改用户凭据。后续又找到更窄的原生公开生命周期释放方法，可在有界 ASGI finally 中调用而不改私有计数器；其内部使用 request-stash lock、slot ID 和幂等释放，独立已核对精确版本源代码。采用此修复可以保留既有可选 key 限制。无论选哪条路径，都必须使用同一 key 连续强制超时后再成功、其他进行中请求不被误释放，以及全局第三请求 429 的回归。最终结果应由之后完整 canonical 回归确认，本段不提前宣称完成。

实际选择了更窄的生命周期补齐：保留每-key 1/2 和全局 2，ASGI finally 调用精确版本原生方法，独立 1 秒预算，失败关闭。独立复核确认没有生产计数器减一/flush，也没有从客户端数据重建待释放身份。针对同一 key 的 deadline、断连、pre-deployment 取消，以及其他请求占用的隔离回归均保留原断言；恢复后的原 key 值仍为 2，三轮 deadline 后均能立即正常调用。专项证据见 [原生槽清理](native-request-slot-cleanup.md) 与 `operations-native-recovery.json`。上述修复现已通过冻结版本的最终完整 canonical 回归与恢复演练；最终签核见文首。

## 最终时限与串行负例夹具校准

持续流测试原先把 sub-second 时限同时用于启动/首正文等待，混入了环境速度与前置空事件延迟。最终把该隔离场景的配置总时限设为 2 秒，首个真实上游事件即有 role+Hello，此后每 0.2 秒持续输出，真实正常终态在 4 秒以后。断言仍严格要求 wire elapsed 为 1.8–2.45 秒、保留 Hello、没有 DONE/stop、没有备用、首事件早于 deadline、终态为 stream_interrupted/deadline_exceeded、资源释放及同 key 下一次健康调用。2.45 秒上限排除了把两次 2 秒预算相加的错误实现；生产默认 90 秒及尝试上限没有改变。

串行参数拒绝测试在每次 400 后等待必要审计 finally 释放 admission，再检查下一种独立非法输入；否则后续输入可能合法遇到仍在进行的清理所占并发，而得到 429。独立的真实重叠并发测试仍保留两个实际活动请求及第三个 429 的断言。这些是隔离被测条件，未通过扩大生产时限、清缓存、替换 key 或删失败断言掩盖缺陷。
