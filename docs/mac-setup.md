# Mac 快速开始：先让个人应用接上模拟网关

这条路径适合个人自用与基本 AI 应用开发。源码已准备好，但没有在你的 Mac/Docker Desktop 部署。模拟成功不代表真实供应商资格或 Mac 容器验收通过。

## 1. 准备

- 使用已经安装的 Python 3.12/3.13。预检只需标准库，不安装包、不创建凭据、不改设置。
- 安装并打开适合自己芯片的 [官方 Docker Desktop](https://docs.docker.com/desktop/setup/install/mac-install/)，由你核查系统要求及 [许可条件](https://docs.docker.com/subscription-billing/desktop-license/)。不要关闭系统安全功能。
- 源码放在自己的目录，不复制云测试 `.venv`、数据库或临时凭据。

在仓库根复制空模板，已有文件拒绝覆盖：

```sh
(umask 077; set -C; cat .env.example > .env.mock)
```

用本地编辑器填写 `.env.mock`。四个数据库密码须由你认可的密码管理方式准备在独立私有文件中，填写对应的绝对路径，不把密码写进命令行：

- `POSTGRES_BOOTSTRAP_PASSWORD_FILE`：仅新数据库卷初始化
- `POSTGRES_MIGRATION_PASSWORD_FILE`：schema 维护/恢复
- `POSTGRES_RUNTIME_PASSWORD_FILE`：应用日常读写
- `POSTGRES_BACKUP_PASSWORD_FILE`：只读备份
- `DATABASE_URL`：`postgresql://gateway_runtime:<URL编码后的runtime密码>@postgres:5432/gateway`
- `MIGRATION_DATABASE_URL`：`postgresql://gateway_migrate:<URL编码后的migration密码>@postgres:5432/gateway`
- `LITELLM_MASTER_KEY`：既有管理员密钥，`sk-` 开头且至少 32 字符
- `LITELLM_SALT_KEY`：独立的稳定盐值，至少 32 字符，备份恢复时保持一致
- `CONSUMER_PORT=4100`、`ADMIN_PORT=4101`

保留模板中的 `DATABASE_NAME=gateway`、`MAINTENANCE_DATABASE_USER=gateway_migrate`、`BACKUP_DATABASE_USER=gateway_backup`。四个数据库密码、管理员密钥和盐值共六份值必须互不相同；密码文件包含一行 16–256 字符的可打印 ASCII，可有一个结尾换行。长度检查不证明随机性。

私有文件须是自己拥有的 0400/0600 单链接普通文件，父目录不可他人写入；拒绝符号链接、硬链接、设备和相对密码文件路径。`.env.mock` 接受模板中的固定键、字面 `KEY=value` 和单/双引号，不接受变量插值、反引号、转义、重复键或额外覆盖。不要 `source .env.mock`。不填任何真实供应商 key，不把文件/值发到聊天或工单。

## 2. 预检并查看七步启动计划

```sh
python3 ops/mac_preflight.py plan
# 机器可读：python3 ops/mac_preflight.py check --json
```

预检只读，分别显示 `PASS`/`FAIL`/`NOT_RUN`。全部通过才会给计划，仍须逐条审阅、执行，不要管道给 `sh`。七步为：静默校验 Compose → 构建网关/模拟上游 → 启动独立 PostgreSQL → 用独立 migrate 服务迁移并赋权 → 启动 mock/gateway/ingress → 检查 Nginx → 查看服务状态。

每条命令固定 `.env.mock`、两个 Compose 文件、`gateway-mock` 项目和本地 Unix Docker socket，清除继承环境；不会读取生产 `.env` 或旧 active release。`build/up/migrate` 是你执行计划后的实际修改，会下载/构建镜像、创建测试卷并改测试数据库。失败即停。不要把项目名改成生产。

预检同时验证六份秘密相互独立、DSN 与对应密码文件一致、角色固定、端口不同且未占用、关键部署/角色脚本指纹、Docker/Compose 2.20+ 与 Linux 引擎。端口探测不预留端口；配置指纹仅用于漂移检测。发现 Docker/Compose/config shell 覆盖会阻止计划。不得为了通过而自行刷新指纹或随意修改 socket 权限。

新角色初始化只适用于新卷。旧版共享身份数据卷需要独立迁移审查，不能简单重用，也不要 `down -v` 删除后宣称升级成功。

## 3. 创建一把应用受限 key

启动后先检查无密钥消费请求被拒绝，且消费端口不能访问 `/key/generate`。完整生命周期见 [运维手册](operations-complete.md)。

先新建 0700 私有目录；若已存在，先核对它确实属于本项目且安全，不要覆盖或放宽权限：

```sh
mkdir -m 700 secrets
```

账本初始化默认预览：

```sh
python3 ops/key_admin.py ledger-init --target mock --env-file .env.mock \
  --ledger-file secrets/revocations.mock.jsonl
```

明确批准并确认没有遗漏的历史撤销后，加 `--execute --ack-key-change --ack-ledger-history`。新空模拟栈可确认没有既往撤销。账本和 `.head` 高水位独立保存，不能跟旧数据库一起回滚。

再预览受限 key：

```sh
python3 ops/key_admin.py create --target mock --env-file .env.mock \
  --ledger-file secrets/revocations.mock.jsonl --name local-app \
  --deployment-id mock-primary --deployment-id mock-secondary \
  --key-file secrets/local-app.json
```

批准具体凭据创建后才加 `--execute --ack-key-change`。输出文件必须不存在；密钥只写入私有文件，不打印到终端。文件绑定 mock 管理端口和账本 UUID，后续 info/delete 必须携带同一账本；误拿其他环境文件会在网络前拒绝。旧版无绑定文件按运维手册独立核查并 bind-existing，不能直接删除。请求结果不确定时保留占位，先私下核对服务端状态，不盲目重试。

## 4. 应用接入

- Base URL：`http://127.0.0.1:4100/v1`
- 模型：`general-free`
- API key：刚创建的受限调用密钥；不要使用管理员密钥。附带示例可用 `--key-file secrets/local-app.json` 直接私密读取，无需粘贴或export到shell，见 [协议示例](protocol.md)
- 先测普通文本、流式和一次错误响应；只用非敏感固定样例

默认没有真实模型出口。供应商启用须另外完成免费资格、隐私、权限、网络与首消费应用验收，见 [接入清单](provider-onboarding.md)。支持范围以本项目协议契约为准，不能把“OpenAI 风格 URL”当成所有 API、工具调用和参数都兼容。

## 5. 查看状态、备份和停止

实际只读状态（Docker Desktop 的 socket 按预检确认结果填写）：

```sh
python3 ops/gateway_ops.py status --target mock --env-file .env.mock
```

备份、排空、发布与恢复也都已支持显式 mock 目标，详见 [恢复流程](operations-complete.md)。恢复必须保持 drain，重放独立撤销账本，核对 helper 以外的历史，再明确 resume。真实 Mac/Compose 流程仍需单独演练。

停止时复制计划中同样的 `env ... docker ... compose` 前缀，结尾用 `down`。保留卷，不追加 `-v`，除非已独立批准不可恢复地删除这些测试数据。

## 6. 仍须你本机确认的几件事

1. 真实 Mac 芯片、macOS、镜像架构/摘要和 Docker 组合可运行
2. Nginx 配置检查、管理/消费隔离、数据库不暴露端口
3. 正常流式/非流式、失败备用、断流、取消、并发限制
4. 撤销立即生效、数据库中断失败关闭、恢复后旧 key 仍拒绝
5. 完成一次容器卷备份/恢复与维护发布，再接真实供应商

预检单测使用模拟平台/Docker；临时 PostgreSQL 原生测试是真实 Linux 进程，但两者都不是 Mac 已通过。遇到故障先保留脱敏报告、命令名称与退出码；不要贴原始环境或未审查日志，不跳过鉴权或 schema 检查。
