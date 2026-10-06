# 原规格逐项验收矩阵：完整工程候选

依据 [原始规格](../docs/source-spec.md)，更新于 2026-10-06。用户已明确个人自用、基础 AI 应用；附带官方 SDK 示例作为第一个消费端，不另设商业 IDE 或尚未编写应用的前置门槛。

**这里区分“源码/隔离真实进程验证”与“用户 Mac/真实账户上线”。** 测试使用合成上游，PostgreSQL/Proxy/Squid是实际进程；没有访问真实账户、生成真实供应商凭据或在 Mac 部署。最终源版本的执行摘要见 [test-results.md](test-results.md)，独立原规格复核见 [original-spec-independent-audit.md](original-spec-independent-audit.md)。

## 证据层

| 代号 | 证据 |
| --- | --- |
| C | contract/security/state/metadata/quota 单元与真实TCP Router；MemoryState或明确故障注入不当成数据库证据 |
| P | tests/integration/test_proxy_postgres.py：原生适配器、真实Proxy/PG/TCP、原生key与安全 |
| S | tests/contract/test_sdk_bridge.py、test_sdk_bridge_config.py、tests/integration/test_sdk_proxy_postgres.py：官方SDK窄适配，含原始usage严格类型与公共流拒绝/故障 |
| N | tests/integration/test_proxy_boundaries.py：独立原生进程时限/备用历史；旧native拒绝缺陷只作复现 |
| A | tests/integration/test_sample_consumer.py：附带官方SDK应用到真实Proxy/PG，非流/流/错误/取消 |
| D | tests/integration/test_proxy_diagnostics.py、test_trace_query.py、test_retention_failure.py、ops/test_diagnostic_export.py：真实历史/分页/时延与daily TTL，私有导出与清理失败 |
| B | tests/test_downstream_backpressure.py：ASGI挂起发送/清理/close和真实TCP慢读 |
| R | tests/verify_native_recovery.py、verify_backup_restore.py、native-recovery.json、database-recovery.json：实际进程/DB重启、A→B→A、活跃key恢复、撤销账本重放、失败restore事务回滚、migration护栏 |
| O | ops全套测试：目标/凭据/overlay隔离、失败维护顺序、manifest/账本/checkpoint/清理；Docker transport的替代测试不当成Docker执行 |
| DB | tests/verify_database_roles.py、database-roles.json：四SCRAM角色、非超级迁移/恢复、runtime权限拒绝和原生key/推理 |
| E | supply-chain/*.json、tools/supplychain_verify.py、tests/test_supplychain.py：实际摘要/公告/许可与源码完整性、官方Squid7.7编译及19项TCP/ACL；不是最终Mac镜像证据 |

## AT-01 至 AT-24

| 编号 | 当前证据与已实现结果 | 仍需现场的范围 |
| --- | --- | --- |
| AT-01 端点/隔离 | P/S：两个公开端点、未知别名、无效/管理员/消费身份隔离；Nginx精确路径声明 | Mac的Nginx实际入口/端口/网络组合 |
| AT-02 首消费端 | A：examples.client真实连Proxy/PG，非流/流/错误/取消；SDK2.54.0，max_retries=0，30秒客户端超时 | 真实模型账号联调；没有额外商业客户端前提 |
| AT-03 顺位/实际目标 | C/P/S/N：两候选顺位、429后一次备用、公共模型/部署/诊断关联 | 两个真实模型及独立账户/池的核查 |
| AT-04 分片/空块/ID/usage | S/A：标准流无文字重复/丢失，role/empty/refusal/合法空终态保留；公共stream usage未知，私有仅可信源认证 | 真实模型流与实际Mac入口；include_usage是原规格可选，不伪造支持 |
| AT-05 分阶段断流 | S完整Proxy：首事件前有界备用；角色后/正文后无第二渠道，无stop/DONE伪成功；N旧native路径分别留证 | Mac Nginx和真实供应商组合 |
| AT-06 参数能力 | C/P/S：白名单、严格类型、深度畸形JSON、主备能力交集和逐次检查，不丢关键字段 | 每个真实候选的角色/输出/上下文能力 |
| AT-07 免费禁区零调用 | C/P/S：未知/付费/过期/停用目标主备零调用；生产初始全部停用 | 真实账号免费依据/费用硬边界，不能由代码验证人为事实 |
| AT-08 空池/拒绝/截断 | S：空池503；独立refusal、正文后refusal、同块拒绝、length准确；旧native流schema禁止生产 | 实际供应商输出与资格；旧缺陷不再是推荐SDK工程路径阻塞 |
| AT-09 用量/观察来源 | C/S/D：原始三项整数、缺失/零/部分/null/bool/string/float/负值；unknown/estimate/expired不补零满额 | 真实供应商值和账单；观测Token不等于余额/费用证明 |
| AT-10 完整历史 | C/N/S：主备消息完全保序、输出上限精确映射；过长输入只拒绝，无隐藏裁剪 | 真实Token边界不作未测保证 |
| AT-11 429/共享范围 | C/P/S：相关池冷却、Retry-After源与已知等待、独立池不误伤、同池受控探测；D审计冷却范围 | 实际账户共享范围/上游头语义 |
| AT-12 次数/总时限 | N/S/A：SDK/Router重试0，最多两次实际调用，共享总预算；慢上传/持续流/取消已测 | Mac/Nginx实际组合，用户后续应用不得自行叠加重试 |
| AT-13 多窗口/时区 | C/P：request/day与token/minute、共享/独立scope、Pacific DST 23/25小时与未知重置 | 真实组织/项目/模型额度维度 |
| AT-14 重启/缺失状态 | P/R：真实进程/DB停启与dump恢复保留耗尽和next_probe；丢失/范围变化unknown而非满额；无周期生成探测 | Mac容器卷与服务重启 |
| AT-15 key/备用授权 | P/S：失效key、主密钥、未授权备用、伪造grant都拒绝，未授权目标零调用 | 实际消费入口现场 |
| AT-16 路由/地址/管理 | C/P/S：地址/凭据/文件/metadata覆盖禁止；E真Squid ACL拒绝额外域、IP/私网/metadata/子域等 | 容器绕过代理直连、DNS/TLS/IPv6、入口组合 |
| AT-17 key生命周期 | P/S/R/DB：实际创建/并行轮换/撤销、进程重启保持拒绝，旧备份后账本重放；O显式目标/私有文件 | 你Mac上实际受限key授权/保存与容器重启 |
| AT-18 全部尝试追踪 | D/P/S：候选历史排除、实际目标/顺位、耗时/首事件、重试/冷却、终态与同一request/revision；拒绝原文不存 | 现场部署日志链路；不推断供应商内部resolved型号 |
| AT-19 日志/导出/保留 | D/P/O：正文/key/恶意错误不泄露；真PG事件/daily TTL；清理失败显式不ready；0600导出；明确opt-in备份清理；Nginx不另留access副本 | Mac文件权限/日志驱动、正式备份加密与生命周期执行 |
| AT-20 发布/覆盖/回滚 | P/N/R/O：DB配置冲突拒绝；实际A→B→A；无效candidate/激活失败恢复旧版并保持drain；overlay/镜像/schema护栏 | 完整Docker维护CLI事务与镜像构建；命令替代测试不冒充执行 |
| AT-21 备份/迁移/DB中断 | R/DB：189原生迁移，真实dump/restore/恢复后身份与推理，实际失败restore事务回滚，未完成migration拒绝，DB停机失败关闭 | Mac容器恢复；任意跨schema升级/降级不自动支持，按手册独立演练 |
| AT-22 取消/慢读 | A/P/S/B：官方SDK取消、TCP断开与完全不读、挂起close；停止后续尝试并释放本地槽；版本绑定原生槽清理，同key多轮取消/超时后继续成功 | Nginx/真实上游组合；不保证供应商已停止计量 |
| AT-23 并发/探测 | P/S/C：2占用第3个429，key1重复拒绝且不双释放，恢复原key2三轮deadline后仍成功；共享池原子状态与单探测 | 目标机持续压力；跨实例不是本次范围 |
| AT-24 隐私/伪造配置 | C/P/S：隐私grant/资格/逐尝试、嵌套覆盖/头/路由注入拒绝；无正文敏感识别冒充 | 真实账户地区与条款核查，默认仅非敏感学习测试内容 |

## FR-01 至 FR-23

| 编号 | 工程实现映射 | 结论 |
| --- | --- | --- |
| FR-01 | AT-01/02，统一入口+附带应用 | 实现并验证；Mac入口现场待验 |
| FR-02 | AT-03，general-free与实际请求目标关联 | 实现；真实模型资格待核查 |
| FR-03 | AT-04/05/22，SDK窄适配/取消/终态 | 推荐路径完整合成链路验证；旧native流明确禁用 |
| FR-04 | AT-06，严格输入/能力交集 | 实现并验证 |
| FR-05 | AT-07/15/24，每次免费/授权/隐私 | 机制验证；真实资格事实外部核查 |
| FR-06 | AT-07/16，无收费备用/覆盖 | 代码及出口组件验证；容器网络待验 |
| FR-07 | AT-15/17，原生受限key/轮换/撤销 | 原生实际生命周期验证；真实持久访问需授权 |
| FR-08 | AT-05/08，失败不伪成功 | SDK推荐路径拒绝/截断/中断通过 |
| FR-09 | AT-09，usage来源与未知 | 实现并验证；公开流usage未知是允许行为 |
| FR-10 | AT-10，保序/无裁剪 | 实现并验证 |
| FR-11 | 工具/结构化能力独立扩展 | 原规格L，不实现，明确400 |
| FR-12 | AT-03/11，先筛选再顺位 | 实现并验证 |
| FR-13 | AT-11/12，2次/统一时限 | 实现并验证 |
| FR-14 | AT-11/13，429/耗尽/范围 | 实现并验证，真实scope待核查 |
| FR-15 | AT-09/13，来源/时间/未知 | 实现，不造真实余额 |
| FR-16 | AT-14/23，持久恢复/单探测 | 原生进程/DB验证，容器待验 |
| FR-17 | AT-18，完整事件字段/请求时间线 | 实现并验证，私有筛选导出 |
| FR-18 | AT-18/19，少量元数据/保留 | 代码/真库验证，Mac正式文件生命周期待验 |
| FR-19 | AT-20，配置唯一源/发布/回滚状态 | 实现；原生进程实测，Docker事务待验 |
| FR-20 | AT-20/21，固定版本/回归/备份 | 源码与真库验证；最终镜像/现场升级仍独立门槛 |
| FR-21 | AT-16/17，入口/目标/角色隔离 | 组件和角色验证；实际容器组合待验 |
| FR-22 | 六模块通过现有API/CLI/参考应用 | 原规格S已补基本操作路径，不新增网页 |
| FR-23 | 多实例共享计数/预扣/对账/编辑流程 | 原规格L，不实现；启动拒绝多worker |

20项M都有明确实现/测试路径；这不等于20项真实生产环境验收已经完成。真实账户/目标机的外部门槛不能从fixture推断。

## G-01 至 G-06 与最终启用门槛

| 门槛 | 当前结论 |
| --- | --- |
| G-01 首消费端 | 附带官方SDK基础应用实际Proxy/PG路径通过；不再阻塞于另选客户端 |
| G-02 主备不越界 | 两适配路径、身份/资格/地址/参数/免费过滤通过；账户事实与Mac网络待验 |
| G-03 流不拼接/不伪成功 | SDK原生完整合成链路通过；旧native流配置禁止；真实模型入口待验 |
| G-04 预算/时限/取消/冷却 | Router/Proxy/DB/SDK/TCP各层通过；目标容器和真实池待验 |
| G-05 全尝试追踪/脱敏 | 历史排除/时延/终态/筛选导出、实际DB与清理失败通过；Mac生命周期待验 |
| G-06 OSS/版本/供应链 | 所需个人功能OSS范围、124包精确锁/公告、基础镜像双架构摘要、官方Squid来源已核查；最终自建镜像OS/npm/Prisma/签名与Mac执行尚缺 |

完成真实启用还需：用户Mac连接与获准本机执行；独立私有凭据安全准备；真实免费/隐私/模型/池审核；最终镜像及入口出口/容器维护现场验收。其余首版之外能力不作为新的“下一版”问题反复交给用户。
