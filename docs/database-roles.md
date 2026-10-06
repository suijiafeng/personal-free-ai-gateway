# PostgreSQL 最小权限与独立凭据

本候选版本把数据库引导、迁移/恢复、运行、备份分成四个登录身份。此次交付未生成真实密码或修改已有真实数据库，也不证明目标 Mac 已部署。旧版本目录保持不变。

## 身份与边界

- `gateway_bootstrap`：仅 PostgreSQL 首次空卷初始化使用的超级用户，拥有数据库 `gateway`。密码只挂到 PostgreSQL 容器；gateway 和 migrate 均拿不到。
- `gateway_migrate`：非超级用户，无 CREATEDB、CREATEROLE、REPLICATION、BYPASSRLS。拥有 `public`、`gateway_ext` 和其对象，具有目标数据库内创建 schema 的权限，用于原生迁移和恢复。它不创建、轮换真实凭据。
- `gateway_runtime`：仅 schema USAGE、业务表 SELECT/INSERT/UPDATE/DELETE、序列 USAGE/SELECT。无数据库 CREATE/TEMP、schema CREATE、TRUNCATE、DDL、角色成员关系、角色管理权限。原生 `_prisma_migrations` 仅可读。
- `gateway_backup`：仅 schema USAGE、所有业务表和序列 SELECT，包括原生迁移记录。另设置 `default_transaction_read_only=on`；真正边界由 ACL 提供，即使关闭此会话默认值仍不能写表。备份可包含虚拟 key 等敏感数据，仍须加密、限权和遵守恢复手册。

四个密码必须互不相同，且不得复用 LiteLLM master key、盐、上游渠道 key、备份加密身份或主机登录凭据。数据库用户名不是保密材料。SQLite、无密码生产模式、超级用户 runtime 均不是替代方案。

## 配置

`.env.example` 保留空秘密字段。经用户授权的秘密流程先准备四个独立文件，然后填写其绝对路径：

- `POSTGRES_BOOTSTRAP_PASSWORD_FILE`
- `POSTGRES_MIGRATION_PASSWORD_FILE`
- `POSTGRES_RUNTIME_PASSWORD_FILE`
- `POSTGRES_BACKUP_PASSWORD_FILE`

Compose 分别挂载为 `/run/secrets/postgres_bootstrap_password`、`postgres_migration_password`、`postgres_runtime_password`、`postgres_backup_password`。引导脚本拒绝空值和重复值，不把密码放入命令行，也不输出密码。新增 root-before-drop 入口桥只在空新卷运行；它先验证独立 tmpfs 挂载、非符号链接目录及普通私有来源文件，经已打开文件描述符复制三份角色密码，校验源 inode/权限/大小/时间戳未变化，再设置目标文件为容器 postgres 所有、0400，目录为 0700。全部成功后才调用未修改的官方 PostgreSQL entrypoint。

`DATABASE_URL` 必须使用 `gateway_runtime`；`MIGRATION_DATABASE_URL` 必须使用 `gateway_migrate`。两者数据库为 `gateway`，Compose 网络主机为 `postgres:5432`，各自密码须与对应文件一致，并正确 URL 编码。禁止在迁移 URL 添加 libpq/Prisma 参数覆盖。维护工具固定校验 `DATABASE_NAME=gateway`、`MAINTENANCE_DATABASE_USER=gateway_migrate`、`BACKUP_DATABASE_USER=gateway_backup`。

`.env` 本身仍包含两条私密 DSN、master key 和盐，须限制访问，不能提交、打印、贴到聊天或收集入证据。Compose inspect/config 也可能暴露环境秘密，不应保存未经脱敏的输出。四个秘密文件不进入备份证据。

Docker Compose 本地 file-backed secrets 是 bind mount，不保证按声明重映射 uid/gid/mode；不能让降权后的 postgres 用户直接读取主机用户私有文件。原文件保持主机用户所有、0400/0600，不放宽权限。只有容器入口 root 读取原文件；官方入口读取 bootstrap 密码，三份额外角色秘密只暂存在专用 64KiB tmpfs，init 脚本读入后立即删除。其密码环境变量仅供短命 psql 子进程，使用后清除，不进入长期服务父进程环境；已有卷不读取或暂存额外角色密码。暂存失败不会进入官方 init；init 失败则停止启动，任何残留只在 tmpfs，不能当作初始化成功。目标机器仍须核验 root 读取、降权读取及清除全链路。秘密轮换需要单独授权、角色密码更新和相应 DSN 更新；更换文件不会自动更新数据库角色密码。

## 首次启动和迁移

1. 仅全新空数据卷由 `entrypoint.sh` 安全暂存三份角色密码，再交给官方入口降权并运行 `10-roles.sh` → `roles.psql`，建立四身份边界、schema 所有者和默认授权。脚本打包执行位保持 0755，SQL 使用固定绝对路径，即使 shell 脚本被 source 也不会误找文件。原生镜像主机 TCP 认证使用 SCRAM-SHA-256。
2. `migrate` 一次性服务只收到迁移 DSN；运行固定版本原生 `prisma migrate deploy`，再执行 `20-gateway-ext.sql` 和 `30-grants.sql`。
3. 该服务验证实际数据库登录角色及其权限、两 schema 所有权。失败即终止；不会 `db push`、强制接受数据丢失、自动 resolve migration history。
4. 角色 DDL 连接只在本会话关闭 statement logging、把 error-statement logging 阈值调到 panic；不改变全局日志配置。故意失败的重复角色 SQL 已验证不会把密码写到客户端或 PostgreSQL 日志。gateway 等待 postgres 健康和 migrate 成功完成，随后只用 runtime DSN 启动。`DISABLE_SCHEMA_UPDATE=true` 禁止原生 Proxy 启动时更新 schema；扩展表 DDL 已移出运行态。
5. production runtime 还在数据库检查实际登录身份、角色高权限标志、DDL/所有者/角色成员权限。发现错误配置会 fail-closed，不能仅靠 DSN 用户名自证权限安全。

mock overlay 继承同一角色隔离机制，但使用独立 Compose 项目和独立配置/秘密/卷。不可把 production 的 `.env` 或密码文件当作 mock 输入。原生集成单元夹具中的 `gateway_test` 超级用户仅存在于另一个明确标识的临时回归数据库；它不能代表最小权限验证通过。

## 备份和恢复

维护工具通过 `docker compose exec -T postgres` 内部读取指定秘密文件为短命进程的 `PGPASSWORD`，固定 `-h 127.0.0.1` 走 TCP/SCRAM。备份和 schema 只读检查使用 `gateway_backup`；恢复使用 `gateway_migrate`。密码不放到 `pg_dump`、`pg_restore`、`psql` 参数中。

备份/恢复使用 `--no-owner --no-acl`，对象由维护身份拥有，随后必须在恢复事务成功、重新启动 gateway 前执行：

```sh
docker compose --env-file /authorized/profile.env -f deploy/compose.yaml \
  run --rm --no-deps --no-build --pull never migrate --grants-only
```

实际运维应使用 `ops/gateway_ops.py` 的 production/mock 目标和配置选择，让维护锁、drain、撤销记录重放和恢复检查一起生效。上面的命令仅说明授权重建接口，不是完整恢复流程。

`--grants-only` 不执行 schema 迁移；它只校验正确 schema 所有者并重新应用 ACL、默认授权及 migration history 写入禁令。runtime 永远不能重新授予自己 DDL。

## 已有卷与升级

既有 `POSTGRES_USER=gateway` 超级用户卷不会自动运行新的初始化脚本。直接套用新 Compose 会因缺角色/错误权限而失败，不能视为已隔离。不得自动删除旧卷或重置权限、密码。

对于真实旧卷，先取得用户对维护窗口和凭据变更的明确授权，保留已核验的加密备份与撤销记录。由获授权维护管理员在隔离的新空卷建立四角色，然后用受审查的同版本恢复流程迁移数据、重建 ACL、验证原生 key 和撤销状态。对象所有权/ACL 异常、迁移不完整、秘密文件不可读时保持停止，不能退回超级用户 runtime。跨版本迁移另行评审。

## 可复现验证与外部门槛

静态测试：

```sh
python -m pytest tests/test_database_roles.py -q
```

真实权限测试只创建自己的临时 loopback PostgreSQL 17 集群，不接受外部 DSN：

```sh
python tests/verify_database_roles.py --postgres-bin /authorized/postgresql17/bin
```

需要固定 Python 依赖、Node、官方 PostgreSQL 17 可执行文件以及可写或预准备的 Prisma 缓存。可用 `PRISMA_BINARY_CACHE_DIR` 指向已经准备好的缓存。输出 `evidence/database-roles.json`，不记录凭据、原始数据库行或原始异常。

它验证四角色 SCRAM 登录及错误密码拒绝、非超级用户原生迁移、重复迁移、runtime CRUD/禁 DDL/禁权限提升、原生 Proxy 启动/虚拟 key 创建认证/模拟补全/撤销、SELECT-only 备份、非超级用户恢复和授权重建。项目自带 `examples.client` 已作为个人基础应用的首个 consumer，通过真实 Proxy/PostgreSQL 的非流式、流式、错误和取消验收，不再额外要求另一个 consumer 才算完成。本权限验收不声称已验证 Docker、Compose 秘密挂载、目标 Mac、provider 免费资格或真实零账单；这些仍是外部门槛。


### 首次启动秘密桥的验证边界

`tests/test_database_roles.py` 包含真实隔离 user/mount namespace 与真实私有 tmpfs 的 shell 测试：安全复制、保持源权限、目标 0400/0700、非独立挂载拒绝、目录/来源符号链接拒绝、公开可读秘密拒绝、缺文件失败清理。当前环境只能映射调用者一个 UID；该 fixture 明确使用 UID 0 测试暂存函数，没有冒充不同 postgres UID。Docker 不可用，第二 UID 的切换与官方镜像完整启动尚未实际验收，仍属于目标 Mac/container 门槛。主机秘密没有生成或修改。

参考：[官方 PostgreSQL entrypoint 的 root→postgres 与 init 生命周期](https://raw.githubusercontent.com/docker-library/postgres/master/docker-entrypoint.sh)、[Docker Compose file-backed secrets](https://docs.docker.com/compose/how-tos/use-secrets/)。实际生产镜像仍使用部署文件固定 digest，文档源码阅读不能替代该镜像的启动验收。
