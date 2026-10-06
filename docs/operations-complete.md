# 完整版运维：个人自用，显式目标，失败即停

这份手册替代本目录旧运维命令。V0.1/V0.2 原交付不变。先按 [Mac 模拟环境](mac-setup.md) 跑通附带基础应用，再审查真实供应商。参考应用已经通过原生Proxy/PG合成链路；无需另选商业客户端。

本次代码没有连接你的 Mac、部署容器、创建真实密钥或启用真实供应商。Docker/Compose、Mac、实际供应商与消费应用仍需目标环境实测。自动测试的范围和结果以当前 evidence 为准；临时 PostgreSQL/原生 Proxy 测试不能替代容器部署。

## 1. 最短使用路径

1. 准备 `.env.mock`、四个互不相同的私有数据库密码文件、独立管理员密钥和稳定盐值。只在本地填写，不发到聊天或仓库。
2. 运行 `python3 ops/mac_preflight.py plan`，逐项核对；预检通过后手动按输出启动独立 `gateway-mock` 项目。
3. 在已有 0700 私有目录中初始化撤销账本，再明确批准创建一把只授权 `general-free` 与 `mock-primary/mock-secondary` 的调用密钥。
4. 应用的 Base URL 使用 `http://127.0.0.1:4100/v1`，模型用 `general-free`，API key 使用受限调用密钥。管理员密钥不可用于生成。
5. 验证普通请求、流式、一次错误处理，再按本手册做备份与恢复演练。真实免费渠道另按 [供应商接入清单](provider-onboarding.md) 核对。

## 2. 目标隔离与凭据

每次实际维护都要求 `--target mock|production --env-file 明确路径`，管理端口仅从该私有文件取得。显式 `--admin-url` 必须使用固定 `127.0.0.1`，端口必须与文件一致；其他回环别名不能复用同一账本作用域；禁止代理和重定向。文件不能是符号链接/多硬链接，须为当前用户所有的 0400/0600 普通文件。

| 目标 | Compose 项目 | 配置 | 发布状态目录 |
| --- | --- | --- | --- |
| mock | `gateway-mock` | `config/policy.sdk.mock.yaml` 加 mock overlay | `.runtime/mock` |
| production | `personal-free-ai-gateway` | `config/policy.yaml` | `.runtime/production` |

不会读取旧 `.runtime/active.env`，不会用 shell、另一份 `.env` 或默认远程 Docker context 兜底。已存在旧版发布状态时先核查当前实际配置，再显式发布到新目标状态目录。运行策略 profile 不符立即拒绝。

Docker 默认只连接 `unix:///var/run/docker.sock`。Docker Desktop 可明确追加 `--docker-host "unix://$HOME/.docker/run/docker.sock"`。远程 Docker 不在此简化维护流程中。

已经单独审查并运行生产出口时，维护命令必须明确追加 `--reviewed-egress`，并为每个已启用供应商追加 `--provider groq` / `--provider gemini`。固定栈顺序为 base → egress → Groq → Gemini；只选实际已批准的项，不自动加载其他 overlay、推断供应商授权或添加秘密。mock 禁止这些参数，个人模拟接入不需要它们。

运行前对比所选有效配置与每个已存在服务的 Compose config-hash；gateway 的项目/服务/Compose 文件栈也必须一致。格式2备份还绑定 overlay 文件、有效配置摘要及 egress 的 ACL/approved-hosts/startup 文件摘要。只规范化发布时必然变动的 policy bind 源路径，其余配置漂移或跨栈恢复一律阻断；不会用 base-only 重建带真实出口的应用。Compose config-hash 的语义见 [官方命令说明](https://docs.docker.com/reference/cli/docker/compose/config/)，实际 CLI/daemon 组合仍须在目标 Mac 验证。Compose 版本变化导致 hash 不同时需重新核查，不能忽略检查。

网关单服务发布仅允许保留相同的已启用供应商集合。启用/停用最后一个该供应商部署、增加供应商或改变出口域名，要作为独立维护步骤同步审查秘密挂载、主机白名单和实际加载的 Squid 配置，再验证出口；脚本在排空/停止前拒绝将这类变更伪装成普通 policy release。已有栈的实际 ACL 加载/出口测试也是启用生产维护前的人工门槛。

数据库角色的初始化与实测细节见 [数据库权限说明](database-roles.md)。角色分离：
- `gateway_bootstrap`：只用于新卷初始化，不提供给应用、备份或常规维护命令
- `gateway_migrate`：原生/扩展 schema owner，仅独立 migration/restore 使用
- `gateway_runtime`：日常 DML；无超级用户、建库、建角色、DDL 或 schema owner 权限
- `gateway_backup`：只读备份/schema 查询，默认只读事务

`DATABASE_URL` 使用 runtime，`MIGRATION_DATABASE_URL` 使用 migrate。运维 PostgreSQL 客户端通过容器内 `/run/secrets/postgres_backup_password` 或 `postgres_migration_password` 读取密码，并明确 TCP 连接；密码不放命令行参数。数据库固定为 `gateway`。不同凭据文件、两个 DSN 和各角色权限必须匹配；旧持久卷不会因修改 Compose 自动变成新角色结构，必须另行迁移/演练，禁止删除旧卷来掩盖问题。

## 3. 独立撤销账本

在已有私有目录（下例 `secrets`，0700）中放 `revocations.mock.jsonl`。这不是鉴权数据库，只记录原生 SHA-256 key 标识、UTC 时间、撤销意图/结果和校验链，不存明文调用密钥。完整 hash 仍按私有运维数据保管，不粘贴或公开。

初始化前核对全部历史撤销；如果已有 raw API、UI、旧脚本或其他方式的撤销，账本不会自动发现它们。首次新建空测试栈可确认没有历史。先预览：

```sh
python3 ops/key_admin.py ledger-init --target mock --env-file .env.mock \
  --ledger-file secrets/revocations.mock.jsonl
```

只有明确批准初始化且确认历史已纳入后，加 `--execute --ack-key-change --ack-ledger-history`。工具不会覆盖已有账本。账本、同名 `.head` 高水位和 `.lock` 均为 0600；它们应与数据库备份分开放，单独可靠保存。旧数据库恢复不能覆盖较新的账本/高水位。缺失、损坏、截断、目标/端口或备份 checkpoint 不一致，均拒绝恢复/继续。

创建调用密钥先预览；持久凭据创建要独立批准：

```sh
python3 ops/key_admin.py create --target mock --env-file .env.mock \
  --ledger-file secrets/revocations.mock.jsonl --name local-app \
  --deployment-id mock-primary --deployment-id mock-secondary \
  --key-file secrets/local-app.json
```

批准后加 `--execute --ack-key-change`。输出文件必须不存在，工具不会把密钥打印到终端。新文件包含非秘密的目标 profile/管理端口作用域及独立账本 UUID；`info`/`delete` 都要求同一 `--ledger-file`，在任何网络请求前拒绝错目标或无绑定文件。管理员主密钥轮换不会改变这个绑定。请求结果不确定时保留占位文件，先私下核对，不直接删占位重试。

旧版无绑定文件不能直接用于删除/查询，也不能因为错误目标上返回404/401就宣布已撤销。先独立核对它应属于的环境，再预览迁移本地文件：

```sh
python3 ops/key_admin.py bind-existing --target mock --env-file .env.mock \
  --ledger-file secrets/revocations.mock.jsonl --key-file secrets/legacy-app.json
```

批准该文件/目标核对后，加 `--execute --ack-key-change --ack-bind-existing`。仅当该目标原生 `/key/info` 证实精确 key hash 存在（包括明确deleted记录）才原子补写本地绑定；404、未知状态或已有其他绑定均拒绝，不修改原生凭据。这也不自动补齐此前未记载的撤销历史。

轮换：创建新 key → 应用切换并验证 → `delete` 旧 key → 确认旧 key 被拒绝。撤销意图先写账本并 fsync，随后调用原生 `/key/delete`，再核对原生状态和消费入口 `/v1/models` 的 401/403；超时、5xx、404 不算已拒绝。原生请求失败/响应未知仍保留撤销意图。

```sh
python3 ops/key_admin.py delete --target mock --env-file .env.mock \
  --ledger-file secrets/revocations.mock.jsonl --key-file secrets/old-app.json
```

实际撤销另加 `--execute --ack-key-change`。需要 `.env.mock` 有明确且与管理端口不同的 `CONSUMER_PORT`。本地旧 key 文件保留私有，不自动销毁。

## 4. 备份、发布与回滚

以下命令默认只预览，不读凭据、不连接 Docker、不修改文件：

```sh
python3 ops/gateway_ops.py backup --target mock --env-file .env.mock \
  --ledger-file secrets/revocations.mock.jsonl
python3 ops/gateway_ops.py release --target mock --env-file .env.mock \
  --ledger-file secrets/revocations.mock.jsonl --candidate config/policy.sdk.mock.yaml
```

实际 backup 加 `--execute --ack-maintenance`；实际 release/rollback 再加 `--ack-compatible-schema`。Docker Desktop 的 socket 参数也要显式添加。rollback 使用同一流程，但 `--candidate` 必须明确选择先前 policy；不会猜版本、降低镜像或数据库版本。

发布先校验候选，再排空存量请求，备份，停止应用，选择不可变 policy，使用现有镜像重建，核对 revision/数据库，重放账本后才恢复流量。没有 build/pull/隐式升级。mock 发布目录同时保留 overlay 要求的 `policy.sdk.mock.yaml`。

备份目录为私有 0700，dump/policy/manifest 为 0600。完成 manifest 最后写入；缺失 manifest 即未完成。文件数据和完成记录经 fsync 落盘。format 2 记录目标、policy/dump SHA-256、精确 PostgreSQL 版本、gateway 实际镜像 ID、已完成 migration 的名称/校验和摘要、独立账本 checkpoint。排除 `.env`、密码文件及账本副本；dump 本身包含敏感原生鉴权/运维数据，仍需访问控制和独立加密存储。

候选激活失败时先确认停止应用，重新建立 drain，再核对当前 migration/镜像仍与安全备份一致，才恢复先前 policy。恢复成功也返回失败，并保持 draining；失败不能写“发布成功”。schema/镜像已变更或状态未知时不强行启动旧应用。

## 5. 恢复与恢复流量

自动恢复要求当前栈仍可排空，并能读取当前数据库制作安全备份；支持已验证的同目标/同Compose栈、相同 PostgreSQL 精确版本、相同 gateway 镜像和相同 migration 基线。数据库完全不可读、服务无法安全排空、未知或未完成 migration 时不会自动覆盖。此时保留原卷、备份和独立账本，先按隔离新库/已批准角色重建的人工灾难恢复流程核查；本CLI不声称实现无人值守灾难重建。仅确认参数不能绕过这些检查。旧 format 1 缺少必要证据，自动恢复被拒绝，须单独演练。跨版本升级/数据迁移属于独立变更，先在隔离数据库验证；不得使用 `db push --accept-data-loss` 或自动标记失败 migration 已解决。

先预览：

```sh
python3 ops/gateway_ops.py restore --target mock --env-file .env.mock \
  --ledger-file secrets/revocations.mock.jsonl --archive backups/mock/明确目录
```

实际恢复需完整确认：`--execute --ack-maintenance --ack-compatible-schema --ack-database-replace --ack-revocation-history`。最后一项表示你已独立核对 helper 之外的撤销历史；它不是自动验证结果。未能重建完整历史时不要恢复流量。

流程：校验 → 排空 → 另做当前安全备份 → 停应用 → 原生 `pg_restore --single-transaction --exit-on-error --clean --if-exists --no-owner --no-acl` → `migrate --grants-only` 重新限定权限 → 验证 migration/revision/数据库 → 从独立账本经原生 API 撤销旧快照复活的 key → 保持 drain。

事务恢复失败不会留下半份 SQL 写入，但整体流程仍标为未完成；权限重建、账本重放或启动验证失败同样不恢复流量。`maintenance-state.json` 保存未完成标记。检查旧 key 的实际拒绝、额度观察、重置时间和 live key 权限后，单独 resume：

```sh
python3 ops/gateway_ops.py resume --target mock --env-file .env.mock \
  --ledger-file secrets/revocations.mock.jsonl --expected-revision 明确已核对的版本
```

执行时加 `--execute --ack-maintenance --ack-compatible-schema --ack-revocation-history`。若先前维护未完成，还必须提供安全备份 `--archive` 和 `--ack-recovery-review`，且运行 revision/schema/image 与该备份一致；未启动的应用先按已核查的安全版本私下恢复到 drain，再执行检查。不删除未完成标记冒充验证。

账本自动化只证明“已记录的撤销意图得到核对/重放”，不能证明 raw API、UI 或其他脚本的所有历史都已覆盖。

## 6. 最小保留/清理流程

运行请求元数据按 runtime 的保留策略处理。原生鉴权表、已删除 key 记录、原生审计记录、backup dump 和撤销账本不是同一生命周期，不能把请求事件清理等同于全部历史已清除。原生审计/鉴权表保留按精确版本和恢复要求人工审查；未启用无依据的全表清理。

备份默认不自动过期。只有新建备份时显式加 `--retention-managed` 才允许专用清理工具管理；默认历史、手工目录、其他目标、旧格式及撤销账本不纳入。预览：

```sh
python3 ops/backup_retention.py --root backups/mock --target mock \
  --older-than-days 30 --keep-newest 2
```

先审核输出中的精确目录，再独立批准不可恢复删除，执行时加 `--execute --ack-permanent-delete`。工具至少保留一份最新受管备份，删除前复核所有候选；未知文件、校验不符、符号/硬链接或不安全权限会拒绝。执行失败返回非零并报告未完整清理；不会清账本、高水位、密码或手工历史。中断删除先移除完成 manifest，避免残留目录被当作成功备份。备份上限/30 天是可调整的个人维护选择，不是对合规或永久恢复的承诺。

## 7. 验证边界

- 单测：目标隔离、无 shell 凭据兜底、账本权限/锁/断尾、未知撤销响应保留、migration/image 拒绝、保留范围与真实临时文件删除
- 临时原生 PostgreSQL/Proxy 演练：同版本真实 dump/restore、实际失败 restore 的事务回滚、未完成 migration 护栏、旧快照复活 key、账本经原生 API 自动补撤销、进程重启后旧 key 拒绝
- 单独门槛：Docker/Compose/Nginx 完整部署、目标 Mac/ARM、镜像签名与可复现构建、旧生产卷角色迁移、跨版本迁移、真实供应商资格与首消费应用

没有实际跑到的阶段不写“通过”。发布前保留精确版本、配置 revision、UTC 时间、脱敏结果与失败步骤。
