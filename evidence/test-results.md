# 完整工程候选：最终实测结果

版本 `1.0.0rc1`，2026-10-06 UTC。最终标准 runner **退出码 0**：所有原规格首版可实施代码路径已完成并通过以下工程验收。**尚未在用户 Mac 部署，也未启用真实模型账户。** 所有推理使用本地 TCP 合成上游；测试凭据仅存在于一次性隔离 fixture。

## 1. 同一最终源码的完整回归

全新 loopback-only UTF8 PostgreSQL 17.11 → 原生 Prisma client → **189 项 migration** → 扩展 schema → **8 项独立恢复守卫** → 两个全新 Python 进程的 pytest → 实际原生进程/数据库恢复 → 停止并清理临时实例。

| 阶段 | 主测试 | 子测试 | 失败/错误/跳过 | 耗时 |
| --- | ---: | ---: | --- | ---: |
| 主阶段：全部 tests/ops，SDK Proxy 模块在下一独立进程执行 | 748 | 61 | 0/0/0 | 265.25 秒 |
| SDK Proxy + PostgreSQL 完整模块 | 41 | 10 | 0/0/0 | 52.69 秒 |
| **pytest 合计** | **789** | **71** | **0/0/0** | **317.94 秒** |
| 另行恢复守卫 unittest | 8 | — | 全通过 | 0.028 秒 |

合并 JUnit 的 `tests=860` 包含 71 个子测试；实际 `testcase` 节点 789，不能写成 860 项独立测试。53+11=64 条依赖警告保留，未声称 warning-free。

证据：[合并 JUnit](complete-full-regression-results.xml)、[主阶段原报告](complete-full-regression-results-main.xml)、[SDK 阶段原报告](complete-full-regression-results-sdk-proxy.xml)、[同一次完整控制台](complete-full-regression-console.txt)。两阶段均执行；任一失败、缺报告或恢复失败都会使 runner 失败，没有通过 skip 获得全绿。

完整覆盖：输入与权限边界、实际候选顺位/备用、免费与隐私约束、历史保序、两次总预算、取消/慢读/总时限、原生 key 生命周期、SDK 拒绝/空流/真实终态、严格原始 usage 类型、配额窗口/时区/冷却、诊断查询导出与保留失败、参考应用、运维目标与备份/撤销护栏。逐项范围见 [AT/FR 矩阵](acceptance-matrix.md)。

## 2. 实际恢复和取消后继续使用

[原生恢复证据](native-recovery.json) 耗时 **216.059 秒**，完整通过：

- 受限 key 新旧并行、撤销后多次重启仍拒绝；实际策略 A→B→A，核对 revision、路由和持久 drain
- 真正停止 PostgreSQL，缓存有效 key 仍失败关闭且零上游；数据库回来后审计失效的原进程保持关闭，明确重启后恢复
- 真实备份恢复出有效身份、耗尽/冷却与 next_probe；已耗尽主池不会重置成满额
- 备份前撤销的 key 仍拒绝；备份后撤销 key 的复活风险实际复现，再用独立撤销账本重放，在 drain 下恢复拒绝，跨进程仍有效
- 未完成 migration 护栏、失败 migration/restore 的事务回滚、未知撤销意图持久保留
- **恢复出的原 key 保持 max_parallel_requests=2 不变；同一 key 连续三轮真实总 deadline 后正常调用成功，无换 key、无计数清空**
- 所有合成 key 最终撤销，临时原生进程与数据库停止

[数据库快照证据](database-recovery.json)：**86 张表、1,377 行**，源/恢复行数及内容摘要完全一致；189 项迁移完成、0 项未完成；2 行活跃 key、22 行删除 key 历史、1 行观察保留；289,276 字节 custom dump 已删除。失败 restore 的部分 DDL 实际回滚。

快照 helper 的 `excludes.restored proxy authentication` 仅表示它自己不测鉴权；恢复后的鉴权由上述原生恢复阶段验证。两层证据不可混淆。更多说明见 [恢复报告](database-recovery.md)。

## 3. 另外真实执行的组件检查

这些检查不重复相加成产品验收数量：

- [数据库角色](database-roles.json)：真实 SCRAM 初始化/迁移/runtime/backup 身份，原生迁移、key/推理/撤销、只读备份与非超级用户恢复；runtime 越权及 TEMP 漂移拒绝
- [DB secret 桥](database-secret-bridge.json)：真实隔离 user/mount namespace 的私有 tmpfs staging，保留宿主私有权限；不同真实 UID 与 Docker 交接未运行
- [供应链/出口](supply-chain-complete.md)：124 个 Python 锁定依赖、官方公告和源码摘要；双架构基础镜像清单；官方 Squid 7.7 实际编译及 19 项 TCP/ACL 试验。没有把源码编译当作最终容器网络验收
- [provider secret 桥](provider-secret-bridge-verification.json)：降权/清 capabilities/NNP/失败关闭的模拟 syscall 回归；实际容器 root→应用 UID 转换待目标机验证
- [独立原规格复核](original-spec-independent-audit.md)、[上手流程复核](onboarding-walkthrough.md)：包括附带 SDK 应用到真实 Proxy/PG、私有 key 文件、目标绑定及维护 overlays

归档前离线复核：[源码/版本/14 项部署指纹/链接/JUnit/旧版不可变](final-static-validation.json)、[供应链完整性](final-supplychain-check.json)、[部署静态门槛](final-deployment-static-check.json)。生产 policy 的 enabled_deployments 仍为空，activation_ready=false。

## 4. 保留失败历史，而非抹掉问题

- 首轮整合暴露同进程重复构造原生 app 会重复注册同一个 Guard，造成零上游 503。已禁止同进程二次构造；标准 runner 按实际部署的一进程一配置分别执行并合并全部用例。见 [根因](native-proxy-process-isolation.md) 和 complete-initial-* 历史。
- 第二轮发现真实原生每 key 并发槽在外层非流式取消后残留，连续两次超时导致后续 429。已补调用锁定版本自身的幂等 request-slot 清理，独立有界；异常即失败关闭。保留 key1/2 约束，以同 key 反复取消/超时/恢复、并发不双释放及恢复旧 key 实证。见 [修复说明](native-request-slot-cleanup.md) 与 complete-second-*。
- 后续集成的两项失败分别是过短预算内正常流预热尚未完成，以及串行非法输入在上次 finally 尚未释放时碰到全局 429。持续流用例配置改为 2 秒，真实终态远在预算外，每块间隔小于 idle，仍要求正文已交付、1.8≤wire elapsed<2.45 秒、无假终态/备用并恢复同 key；生产 90 秒不变。串行负例等待真实清理后再发下一输入，原 400/零上游与独立并发拒绝断言保留。历史保存在 complete-third-*；最终整仓重新全部执行。
- SDK typed usage 会强制转换某些非法数字类型，已改用其公开 API 提供的已解码原始对象校验；SDK 继续负责 HTTP/JSON/SSE，没有自写协议解析或修改 vendor。

旧 native refusal-only 丢失仍作为受限制的旧适配器缺陷复现；推荐 SDK 路径完整 Proxy/PG 验收已通过，生产 schema 禁止 native+streaming。复现缺陷的测试通过不代表旧路径可用。

## 5. 环境、复现与依赖警告

实际环境：Linux x86_64、Python 3.12.14、LiteLLM 1.104.0、OpenAI SDK 2.54.0、PostgreSQL 17.11、Prisma Python 0.15.0/CLI 5.17.0。锁定的部署镜像候选仍须在 Mac 构建验证。

```sh
export LITELLM_LOCAL_MODEL_COST_MAP=True
.venv/bin/python tests/run_postgres_suite.py \
  --postgres-bin /path/to/postgresql/17/bin \
  --junitxml evidence/complete-full-regression-results.xml
```

使用已安装匹配工具；runner 只创建新的临时库。缺数据库的直接 pytest 会明确 skip，不能替代完整 runner；`--skip-backup-restore` 同样不是恢复通过。

64 条警告来自上游 Pydantic ReadOnly/deprecated 字段访问及原生 cooldown callback 未 await；原输出保留，没有压制或改 vendor。项目自己的持久冷却、资源释放和必要审计另有明确 await 与失败关闭验收。

## 6. 仍须现场完成的门槛

1. 用户选定 Mac 上的 Docker build/Compose/Nginx、芯片/镜像架构、secret 跨 UID、入口/出口网络与实际容器维护/恢复
2. 最终自建镜像的 OS/npm/Prisma SBOM、当前漏洞公告及镜像来源/签名核查；现有摘要和 Python 审查不覆盖完整最终镜像
3. 安全准备用户授权的私有凭据，核对真实账户免费硬边界、隐私条款、模型能力和独立额度池，再进行最少真实调用

上述环境与账户步骤尚未执行；任意跨 schema 自动降级、工具/多模态/HA 等也未承诺。个人自用所需核心代码已经完成；“未部署”不能被改写成“生产全部验证”。V0.1/V0.2 原目录清单全部复核不变，旧版报告仅作历史。
