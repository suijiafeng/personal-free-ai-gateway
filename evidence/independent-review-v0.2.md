# V0.2 独立验收复核

复核时间：2026-10-06 09:03–09:07 UTC。依据：[原始需求](../docs/source-spec.md)。

## 结论

**V0.2 的最终独立完整回归通过，仍是工程候选，不是 V1 上线验收通过。**

在全新 UTF8 PostgreSQL 17.11 临时实例上，真实执行 189 项原生 migration、516 项 pytest、61 个 subtests、另计的 8 项恢复保护测试，以及实际原生 Proxy 进程的配置切换、密钥轮换、数据库中断、备份恢复和恢复后鉴权演练。完整 runner 退出码为 0，临时 PostgreSQL 正常停止，临时目录和 dump 已删除。

没有使用真实供应商凭据、真实账户调用、Docker、Nginx 或用户 Mac。生产供应商继续全部停用，生产配置同时关闭 streaming。流式拒绝语义存在已复现的上游缺口，不能把相关测试的通过解释为该能力已经实现。

## 可复现证据

- [独立 JUnit](v0.2-independent-regression-results.xml)：516 passed、61 subtests passed、0 failed、0 skipped；pytest 用时 110.53 秒。JUnit 的 577 是包含 subtests 的计数，不写成 577 项独立测试。
- [独立完整控制台](v0.2-independent-console.txt)：189 项 migration、8 项恢复保护测试、pytest 和原生恢复流程的执行记录。
- [数据库恢复结果](database-recovery.json)：86 张表、396 行，行数与内容摘要一致；189 项 migration 全部完成、没有未完成项。备份包含 2 个活跃原生调用 key 和 11 条已删除 key 记录。
- [原生进程恢复结果](native-recovery.json)：107.46 秒完成的真实进程生命周期演练，与单独的数据库内容恢复分开记录。

运行入口为 `tests/run_postgres_suite.py`，指定 PostgreSQL 17 工具目录和独立 JUnit 路径。本轮使用既有冻结 Python 3.12 环境、LiteLLM 1.104.0、Prisma 5.17.0 及其已缓存匹配引擎；未升级或修改上游安装包。

总数不是产品验收数。尤其 `test_known_native_gap_refusal_after_text_is_lost_on_wire_and_in_diagnostics` 是**已知缺口复现**，不是流式 refusal 产品验收通过。

## 这次真正补上的证据

### 1. 原生服务恢复后的身份与状态

`tests/verify_native_recovery.py` 使用实际原生 Proxy 子进程和独立临时数据库，未用内存鉴权替代：

- 新旧受限 key 在轮换并行期均能实际生成；撤销旧 key 后立即拒绝，后续进程重启仍拒绝。
- 实际执行配置 A→B→A，核对响应头的配置摘要和实际主模型顺序；drain 跨进程保留，解除 drain 前零上游调用。
- 同版本 dump/restore 后启动新 Proxy，备份内的活跃 key 能鉴权，备份前已撤销的 key 仍拒绝。
- 真实复现旧备份会复活备份后撤销的 key。恢复进程先保持 drain，在私有管理链路重新撤销，再验证撤销跨进程重启有效，最后才恢复调用。
- 恢复后保留实测零额度及原 next_probe_at，没有自动补满或推迟重置；真正生成只调用合格备用，不调用已耗尽主目标。
- 真正停止 PostgreSQL 后，已缓存有效 key 的生成也得到 503，零上游调用。因必要审计写入失败，数据库恢复后原进程继续关闭；明确重启原生 Proxy 后才恢复就绪和受限调用。这不是“数据库回来后自动恢复”的证明。

这是单版本、独立 Linux 原生进程证据。它没有执行生产 Docker 维护 CLI、容器卷恢复、镜像升级或跨版本迁移。

### 2. 原生 Proxy 端到端请求边界

新增 `tests/integration/test_proxy_boundaries.py` 的 5 项真实 Proxy/DB/TCP 测试：

- 429 后实际发生主备两次调用，完整 system/user/assistant/user 顺序、内容及输出 token 上限在两次请求中保持不变，尝试链与配置摘要对应。
- 两个慢上游尝试共用全程 deadline，公共响应为 504/deadline_exceeded；恰好两次上游调用，随后取消和本地资源释放，下一次正常请求恢复。
- 持续在 idle timeout 内到达的 SSE，仍被 total timeout 中断；已经输出文本，但没有伪造 stop 或 `[DONE]`，不调用备用，最终请求记录为 stream_interrupted。
- 正常 stop、length、同一 delta 的文本+refusal，公共流与 attempt_finished/request_finished 的 complete、truncated、refused 终态一致，不误标 cancelled。
- 文本之后单独 refusal delta 的缺失，在完整 Proxy 公共流及诊断中均被复现，并断言生产 streaming 关闭。

故障计时前完成一条合成正常预热请求，并等到请求、审计清理和上游资源都空闲后才清零测试计数。首次请求有原生延迟导入成本，不把预热后的测试当作冷启动延迟保证，也不放宽实际故障阶段的次数、结束语义或 deadline 断言。

### 3. 运维入口与失败恢复的代码边界

复核发现并推动修正 Mac 模拟说明与密钥 CLI 的目标错配：模拟部署使用 `.env.mock`/4101，旧 helper 却固定读取 `.env`/4001。新 helper 执行时必须明确选择凭据文件，管理端口必须匹配；不使用 shell 或 active-release 凭据作为隐式备用。最终完整套件包含对应隔离、私有文件、输出预留、不确定创建、脱敏及无代理/跳转测试。

失败配置发布的恢复路径已增加：先停止可能已经解除 drain 的未验证候选，再在共享状态卷建立 drain，恢复已校验的旧配置，核对旧版本与 DB 就绪，并保持 drain。恢复仍记作发布失败，不写成功记录。相关顺序与失败保护在替代 Docker transport 的测试中通过，**实际 Docker 调用没有执行**。

Mac preflight 本轮直接在 Linux 运行，按实际环境返回非 macOS、缺少私有 `.env.mock` 和 Docker 等阻塞，Mac/container acceptance 为 NOT_RUN；不会因纯函数测试通过而显示本机已可部署。配置指纹检查通过，未输出凭据或执行启动计划。

## 复核发现与修正

1. **正常流结束被误记取消**：新增真实 Proxy 测试发现公共流已发出 stop/`[DONE]`，request_finished 却是 cancelled。ASGI 的响应结束后 disconnect 被误作提前取消。最终实现只在最终响应 body 成功发送后标记 response_finished，保留已证实终态；提前断开仍取消。完整 Proxy 三种正常终态和原有真实 TCP 取消回归均通过。
2. **失败发布可能留下未验证候选**：如果候选已移除 drain，随后验证失败，而重新建立 sentinel 的 one-off 容器也失败，旧流程可能没有停止候选。最终实现先停止候选，并测试停止失败时不宣称恢复通过。仍需目标 Docker 实测。
3. **恢复测试对故障后的自动恢复预期错误**：审计失败会把原生进程保持关闭，这是现有安全约束。演练改为证明保持关闭和明确重启恢复，没有为得到绿色测试而取消生产保护。

## 保留的发布阻塞与限制

- **流式拒绝不完整**：LiteLLM 1.104.0 可以丢弃文本之后独立的 refusal-only delta，随后输出正常 stop；已检查的官方 hook 无法恢复丢失信号。合法空流也可能被保守拒绝。生产 streaming 默认关闭，模拟流仅供故障实验。不得将限制描述为“所有拒绝均 fail-closed”。详见 [上游流式复核](native-stream-review.md)。
- 流式 usage 仍为 unknown；不把上游包装层合成值当作实测。需要精确拒绝或已测 usage 时，消费端应明确选择已认证路径的非流式调用；网关不自动重放已提交的流。
- Mac、Docker build/Compose、Nginx 运行、网络出口/TLS、平台镜像摘要/签名和实际消费应用均未验收。
- 真实供应商的账户免费资格、费用硬限制、隐私条款和具体模型能力仍未审核。
- 容器发布失败/恢复、跨版本迁移失败、生产日志/备份生命周期和自动撤销日志恢复仍未完成。
- 本轮保留 42 条上游警告，主要是 Pydantic ReadOnly/deprecation 及原有可选 Prometheus cooldown callback 未 await；不宣称 warning-free。

**可以交付 V0.2 源码和有边界的复现证据，不能描述成已上线、已完成 V1 或已支持完整生产流式语义。**
