# 完整工程候选：数据库与原生身份恢复

2026-10-06 UTC，`tests/run_postgres_suite.py` 最终全新 PostgreSQL 17.11 运行通过。189 项 migration、789 个 pytest 主用例/71 个子测试和另行 8 项恢复守卫均通过后，继续执行本报告的真实恢复。没有连接或替换既有用户数据库。

## 数据库快照层

[database-recovery.json](database-recovery.json) 由 `tests/verify_backup_restore.py` 生成：

- 同版本 pg_dump custom 格式、createdb 新兄弟库、pg_restore 单事务，不覆盖既有恢复库
- public 和 gateway_ext 共 **86 张表、1,377 行**；源/恢复表集合、行数和内容 SHA-256 一致
- 189 项完成 migration，0 项未完成；1 行观察、2 行活跃 key、22 行删除 key 历史一致
- 源表与 dump 共用只读 repeatable-read 导出快照
- 实际注入失败 restore，确认事务内部分 DDL 全部回滚
- 289,276 字节临时 dump 位于私有目录，现已删除；快照恢复阶段耗时 1.831 秒
- 只保存摘要，不保存原始数据库行、key、正文或原始 SQL 错误

该 helper 自身不测恢复后 Proxy 鉴权，JSON 的 excludes 如实保留；鉴权在下一层验证。

## 原生进程、身份和请求生命周期层

[native-recovery.json](native-recovery.json) 由 `tests/verify_native_recovery.py` 生成，耗时 **216.059 秒**：

1. 原生受限 key 轮换并行可用，撤销旧 key 后多次进程重启仍拒绝。
2. 配置 A→B→A：实际 revision 与路由一致；重启持续 drain，核验后才恢复。
3. 主池零额度与 next_probe 保留，快照含两把有效 key。
4. 真正停止 PostgreSQL，缓存有效 key 503 且零上游；审计失效的原进程不会因 DB 回来自动放行，明确重启后恢复。
5. 还原库保持 drain：备份前已撤销身份仍拒绝，有效身份可用；备份后才撤销 key 的复活风险被实际复现。
6. 独立持久撤销账本自动重放，恢复拒绝并跨进程保持；未完成/未知撤销意图不会丢弃。
7. 恢复推理仅选择未耗尽备用，原池余额与 reset 不会被补满/推迟。
8. **恢复的原 key 保持 max_parallel_requests=2；同 key 三轮真实总 deadline 后均正常成功，没有换 key 或清并发计数。**
9. 未完成 migration 拒绝及失败 migration DDL 回滚通过。
10. 最后撤销全部合成 key，停止进程、临时 PostgreSQL，删除临时数据库目录及 dump。

## 隔离与边界

源 URI 只接受固定 fixture 用户、显式端口和 loopback；服务端 data_directory 必须属于 runner 新建临时目录，编码 UTF8、PostgreSQL 17、工具小版本完全匹配。不是仅凭数据库名称判断隔离。

每次 fixture 时间戳和合成标识不同，摘要可变化；每次都要求该次源/恢复完全一致。历史 V0.1 快照保留在 [v0.1-database-recovery.json](v0.1-database-recovery.json)，V0.2 结果见 [历史测试报告](v0.2-test-results.md)。

这些是真实 Linux 进程/SQL 演练，不包括 Docker CLI、Nginx、Mac、跨版本 migration/downgrade 或真实账户。Docker 运维失败顺序的替代测试另列；最终目标机需要独立演练。helper 之外发生的权限/撤销历史仍须明确核对，不能仅凭一把 key 可用就自动 resume。
