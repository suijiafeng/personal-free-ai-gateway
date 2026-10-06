# 新用户上手链路复核

日期：2026-10-06。按用户追加要求，从 README 顺序检查 Mac 预检、私有配置、数据库初始化、mock Compose、受限 key、SDK 示例及维护恢复。不在本轮启动 Docker、连接 Mac、创建真实凭据或调用真实供应商。

## 已直接检查

- README 和 docs 的本地 Markdown 链接全部存在。
- `.env.example` 的固定键集合、空秘密字段和独立数据库角色与 `mac_preflight` 解析器一致。
- 七步 mock 计划顺序与实际文件一致：Compose 校验、构建 gateway/mock、启动 PostgreSQL、显式 migrate、启动服务、Nginx 检查、状态。
- migrate 与 gateway 共用已构建运行镜像；迁移使用独立 owner DSN，应用使用 runtime DSN；mock 策略使用 SDK bridge。
- key 及 backup 的文档 dry-run 命令直接执行成功，没有读取凭据或进行网络/持久访问操作。
- 客户端使用独立 16 包依赖锁；README 提供受限 key 文件输入，无需把密钥贴到命令或 shell history；用户目录创建和 Python 命令已保持一致。
- 所有实际生成、备份/恢复、凭据变更仍有明确执行步骤；预览不伪装成操作完成。

## 本次找到的实质问题

### 1. 容器 UID 与私有 secret 文件权限

主机私有文件要求 0400/0600；不能假设 file-backed Compose secret 会把所有者改成容器应用用户。PostgreSQL 官方入口会在运行 init 脚本前切换到 postgres 身份，原初 `10-roles.sh` 在此后读取 host-owned secret 可能失败。生产 gateway 的 UID 10001 也需要可验证的相同读取路径。

已要求以容器内最小、一次性、内存暂存方式处理跨 UID 读取，保留主机严格权限；不能通过 chmod 0644 放开主机密钥。目标 Mac 的实际文件挂载权限仍须现场验证。角色脚本使用固定绝对 SQL 路径，避免脚本被 source 时 `$0` 指向官方入口的问题；归档亦须保留脚本执行位。

依据：[官方 PostgreSQL 入口源码](https://raw.githubusercontent.com/docker-library/postgres/master/docker-entrypoint.sh)、[Docker Compose secrets 说明](https://docs.docker.com/reference/compose-file/services/#secrets)。这些公开源支持风险判断，不是目标镜像已经运行通过的证据。

### 2. key 文件没有目标绑定

原初 JSON 只有 key/alias/models，误把 production 文件交给 mock 删除时，mock 上“没有此 key”及 401 可以被误判为旧 key 已撤销，原生产 key 实际仍有效。

现有改进为 key 文件写入不含秘密的 target scope 和独立 journal UUID；info/delete 在发出网络请求前核对，缺少或不一致即拒绝。旧文件只允许显式 `bind-existing`，必须由正确目标原生 key/info 确认真正存在，不把 404/未知响应当成依据，也不重标已有其他实例的绑定。该处理继续复用既有文件和账本，不引入新的管理服务。

### 3. 文字步骤和 CLI 的断点

- 增加 `mkdir -m 700 secrets` 的明确步骤。
- 示例支持 `--key-file`，安全读取已有 key JSON；指定文件失败不回退环境变量。
- README 使用与 Mac 指南一致的 Python 调用，并忽略 `.client-venv/`。
- 维护脚本的真实 CLI 入口异常类型重复导入问题已单独修复，错误用脱敏结果表示，避免 traceback。

## 最终验证方式

部署文件变更后须重新核对预检指纹与归档文件模式；key 文件绑定和 legacy 迁移须用真实 CLI/fake transport 或临时原生测试检查错误目标零调用。容器 secret 暂存需有跨 UID 和失败清理证据。最终整仓结果由主测试报告统一记录，不盲目重复相同昂贵测试，也不把静态通过写成 Mac 已完成。

## 本轮独立结果

上述修改后独立执行 `ops`、native recovery guards、client private-key、database roles、provider privilege bridge 和 supplychain 的集中检查：223 项通过、42 个 subtests 通过，8.88 s。证据：`independent-onboarding-results.xml`、`independent-onboarding-console.txt`。随后直接核对预检所有 bundled fingerprints 均一致；PostgreSQL 两个入口脚本均为 0755。

数据库桥的另行真实隔离 user/mount namespace 测试仅验证同 UID tmpfs 和失败清理；不能当成第二 UID 或 Mac Docker 已验证。provider 降权检查当前包含调用模拟和代码复核；目标容器仍须运行只读 `--check-bootstrap`，实际确认私有 secret 可读、永久降权和 capability 清除。未取得这些现场证据前不启用真实供应商。

最后一个窄修正是私有 key JSON 的深层嵌套错误：现在统一转为安全错误，不在示例客户端暴露 traceback。对此及最终目标绑定再次独立复跑 client/key-admin 子集：42 项通过、15 个 subtests 通过，3.61 s，见 `independent-keyfile-final-results.xml`。这是已有检查的定向复跑，不与 223 相加制造不同功能数。

## 最终交付签核

最终冻结版本的 canonical runner 已完整退出0，789个主测试、71个子测试及另行8项恢复守卫通过，failure/error/skip均为0；之后的真实数据库恢复、旧key同值三轮deadline恢复和临时实例清理也已完成。已复核最终 README、START-HERE、测试报告与恢复报告，入口和证据范围一致。个人应用源代码可以交付；真实Mac/容器跨UID、最终构建工件与真实免费账户资格仍须现场完成，不能由本报告替代。
