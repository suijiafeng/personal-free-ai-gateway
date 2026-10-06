# 供应链与部署门槛补齐记录

核查日：2026-10-06 UTC。选定目标仍是用户的 Mac；本次文件编写、元数据读取和测试发生在隔离 Linux 工作区。没有接管或部署到其他机器，没有使用真实供应商凭据，也没有发送真实内容。Mac 当前离线；没有把 Linux 测试当成 Mac/Docker 验收。

## 1. 已经补齐的事实

- `deploy/Dockerfile`、`deploy/Dockerfile.egress` 与 `deploy/compose.yaml` 使用不可变 multi-platform index digest。`deploy/image-candidates.json` 另列出每个 `linux/amd64`、`linux/arm64` 子 manifest 的精确 digest。对注册表原始字节计算 SHA-256，与 `Docker-Content-Digest` 和 index 中对应 descriptor 交叉核对；原始 JSON 位于 `evidence/supply-chain/raw/`。
- `requirements.lock` 保持原有 LiteLLM 1.104.0 / 124 包版本，没有替换流式基线。对每个精确 PyPI 版本查询公告字段：124/124 成功，0 个已知受影响包；快照保存于 `dependency-advisories-current.json`，并绑定 lock 的 SHA-256。这不是 OS、npm、恶意代码或未知漏洞扫描。
- 重新读取 LiteLLM 官方仓库的 17 个公开公告，选定版本均在公开 affected range 之外；机器可读范围和核对结果在 `litellm-advisory-review.json`。GHSA-3cv6-jpf6-8222 的范围字段与说明中的多分支修复版本不一致，1.104.0 高于两者；仍保留入口参数白名单和私有管理入口。
- 增加可以不联网运行的完整性检查、只读部署静态检查、供应链/凭据边界回归测试。不是只增加发布待办。
- 首个消费端已选为随项目交付的 `examples.client`。它已经通过真实 Proxy + PostgreSQL 的非流式、流式、错误和取消集成测试（`sample-consumer-integration-results.xml`，2 项测试包含上述场景）；不再要求用户另选商业客户端或 IDE。此结果使用合成上游，未声称已验证真实供应商模型。

### 实际固定的 index digest

| 镜像 | multi-platform index digest |
|---|---|
| python:3.12.15-slim-bookworm | sha256:7753c33391fc9f01d1984375bf375eb6686d52ba10db6043a86634a5ccf90dcf |
| node:24.21.0-bookworm-slim | sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20 |
| postgres:17.11-bookworm | sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826 |
| nginx:1.30.5-alpine3.24 | sha256:0985e772fb9f729e6fa0980da05fca5d9c468e870eed43071545afa9d2e27d94 |

Intel Mac 对应 `linux/amd64`，Apple Silicon 对应 `linux/arm64`；Mac 的 CPU 架构尚未由本次工作实测确认。固定 index 同时固定两种子镜像，但不能证明任一镜像已经在目标 CPU 上运行。

## 2. 实际实现的受控出口与凭据路径

Nginx 默认 `access_log off`，不额外保存每请求时间/状态/耗时副本。必要调用诊断只在有保留期限的 gateway 数据库里；Docker 日志容量轮转只适用于无正文/身份的系统运行日志，不能冒充七天请求明细 TTL。

默认 `compose.yaml` 仍将 gateway / PostgreSQL 限于 `backend` 的 internal 网络。默认配置没有真实已启用渠道，mock overlay 不引入出口。真实渠道的显式路径为：

1. `compose.egress.yaml` 增加维护中的 Squid，而不是编写新代理。仅 Squid 连接独立 `egress_edge` 网络；gateway 没有直接外网路由，Squid 不发布主机端口，不挂载任何数据库或供应商密钥。
2. Squid 只允许 CONNECT 到精确域名的 443 端口。域名 ACL 使用 `-n` 防止 IP 反查变成域名授权；无通配子域。显式拒绝回环、私网、链路本地、元数据地址、保留和特殊地址，最后 `deny all`。禁用缓存和原始访问日志，不截取 TLS，不注入 CA。
3. `deploy/egress/approved-hosts.txt` 初值 `deny-all.invalid`，不放行任何真实供应商。启动器要求清单严格等于当前已启用且通过资格校验的供应商域名集合；多放一个、少放一个、过期审核或非标准 credential mapping 均失败。
4. Groq / Gemini 分别通过 `compose.groq.yaml`、`compose.gemini.yaml` 授予独立 file-backed Compose secret。`*_API_KEY_FILE` 是文件路径，不是凭据本身。`reviewed_start.py` 清除继承的同名 key 与代理变量，然后仅从 `/run/secrets/...` 加载已启用 provider；缺文件绝不回退到 shell 环境 key。不开 shell、不解释 dotenv、不打印 key。
5. 应用仍进行自己的目标、免费资格、权限和隐私校验。代理放行某个域名不代表某个账号/模型已获批准，也不构成收费许可。

文件式 Compose secrets 不等同专用保险库。主机文件由用户私下准备，保持本人属主、0400/0600 和私有父目录，不建议放宽为 0644，也不依赖 Compose secret 的 uid/gid/mode 自动重映射。

为实际解决宿主属主与容器 UID10001 不同的问题，仅 reviewed egress overlay 使用短暂 root 启动桥，并只授予 SETUID/SETGID，不授予 DAC_OVERRIDE 或 SETPCAP。桥对固定 `/run/secrets/` 下已启用 provider 的文件先 lstat，拒绝符号链接、非普通文件、宽松权限和超长数据；短暂 seteuid 为文件属主以读取原有0400/0600文件，O_NOFOLLOW/NONBLOCK 打开后核对 device/inode/owner/group/mode/size/mtime/ctime，finally 恢复 root。全过程在应用/线程/网络启动前完成，不需要 passwd 中有该属主记录。

随后清空补充组，将 real/effective/saved/fs UID/GID 永久变为10001，清空 effective/permitted/inheritable/ambient capabilities，并从 `/proc/self/status` 验证身份、能力与 NoNewPrivs=1；任何失败都不 exec 应用。边界集中在 `deploy/privilege_drop.py`。只保留的 SETUID/SETGID bounding 位本身不授予能力，空 permitted/inheritable/ambient 集合与 no-new-privileges 阻止 exec 取回权限。reviewed overlay 的 init:false 避免留下 root tini 父进程；healthcheck 也先降权再访问回环。默认 mock / 普通启动继续使用镜像原有非 root 用户。

供应商 key 只进入已经降权的 gateway 进程环境，不进入 Docker Compose 插值、镜像层、磁盘临时副本或诊断输出。Docker exec 默认仍取容器配置用户，诊断必须显式使用 `--user 10001:10001`，只有下述一次性启动桥证明入口以 root 进入。

### 为什么使用 Squid 7.7

Debian bookworm 最新安全包 5.7-2+deb12u6、trixie 6.13 仍被官方 tracker 标为受 SQUID-2026:6/7/8/9 影响；Alpine 3.24 官方包查询返回 7.6-r0。没有为了保留旧 OS 包而忽略已发布修复。

改用上游官方稳定 `SQUID_7_7` 源归档；GitHub release API 给出的 SHA-256 为 `e3bd613b91b1c498ec2992276063342a85cd6edddd5521294e04f44bc055da9b`。已下载并验过归档字节，构建配方再次使用 `ADD --checksum` 固定该归档。`squid-source.json` 保存来源、摘要和验证范围。

官方 47 个 Squid 公告快照保存于 `raw/squid-advisories.json`。GHSA-j9pf-q9f6-v44c 的摘要范围写作 “3.0 - 7.7”，但同条公告的 patched version 与正文均明确 7.7 修复、7.6 及以前受影响；不能机械忽略这个数据不一致。本构建额外禁用 HTTP auth、ICAP、外部 ACL helpers，配置也不使用 `cache_peer`。FTP/明文请求被 CONNECT-only ACL 拒绝，ICP 端口为 0。上述是具体版本/配置审查，不宣称不存在任何漏洞。

已在隔离 Linux x86_64 工作区用官方未修改源码成功编译 Squid7.7，未安装系统软件。使用原始安全 ACL（只改回环绑定端口和临时fixture路径）完成 19 项真实 TCP/配置解析检查：默认拒绝未授权目标、IPv4/IPv6回环、metadata地址、子域、非443、明文HTTP/FTP，以及“已授权域名解析到127.0.0.1”仍拒绝。结果在 `squid-runtime-probe.json`，含二进制、ACL和测试器SHA-256；0真实凭据、0供应商调用。未测试允许域名的公网CONNECT，也未测试Docker网络隔离。

`Dockerfile.egress` 仅构建现成上游程序，没有修改 Squid 源码。它记录构建包版本，但 APT 间接依赖尚非全部内容锁定，不能称为字节级可复现构建。发布必须在目标平台实际构建后固定最终 image digest 并归档 SBOM；供应链离线检查通过不等于该阶段完成。

## 3. 许可证与功能范围

LiteLLM 1.104.0 的版本化 LICENSE 对普通目录使用 MIT，并排除 enterprise 范围。官方功能表将本项目使用的统一接口、virtual keys、master-key auth、fallbacks 和自定义 hooks 列在 OSS 基础能力内。本配置不启用 SSO、企业 RBAC 或其他 premium 功能。

本项目是个人自用的基础 AI 应用。所需功能的官方 OSS 范围已经核查；不要求额外选定商业 IDE，也不把启用可选企业功能、购买企业许可或进行组织级法务审批列为个人基础功能的无期限前置条件。

`litellm[proxy]` 标准安装仍带入 `litellm-enterprise==0.1.71` 和 `litellm-proxy-extras==0.4.102.post1`。这里的“企业功能可选”不表示现有 lock 中企业包不存在或已经移除。不能把 124 包统称 MIT，也不能声称 enterprise Python 模块从不执行。已记录标准 bundle 的 Enterprise LICENSE 摘要指纹；该文本对其软件的生产使用有独立条款，而官方又明确将这里使用的基础功能列为 OSS。这个包级文本与标准 OSS bundle/shared imports 的边界保留为准确的来源说明，不据此笼统判定个人 OSS 使用必须购买许可。若将来启用企业功能、分发编译 bundle，或用于有包级准入规定的组织，再按实际范围核对相应条款。未替用户接受任何协议或购买许可。

`license-scope.json` 区分已核查的个人 OSS 功能范围、标准包的实际许可证，以及扩展用途时需要复核的边界；原有 `dependency-inventory.json` 保留全部 Python 包许可证元数据。`python-lock.cdx.json` 是 CycloneDX 1.6 的 Python 组件清单，不冒充完整容器 SBOM。Squid 为 GPL-2.0-or-later；若将来分发编译镜像，必须同时满足其许可证/源码提供义务。本次交付源码配置与构建配方，没有交付预编译 Squid 镜像。

## 4. 可执行验收入口

在仓库根目录、已安装受检依赖的项目 Python 中：

```sh
python tools/supplychain_verify.py
python deploy/check_reviewed.py
python -m pytest -q tests/test_supplychain.py
```

前两个入口只读；第一个只用 Python 标准库且完全离线。需要重新核对公开注册表时可另行运行 `python tools/supplychain_registry_check.py --online`：它只 GET 已固定的 manifest，不 pull/run 镜像，不落盘临时 token。它核对原始 registry index/子 manifest 字节、平台归属、部署文件是否真正使用 pinned reference、全部锁定包的 SHA-256、公告快照是否匹配相同 lock，以及 Squid 官方源摘要与构建配方。

`deploy/check_reviewed.py` 解析实际 compose/policy/ACL/secret overlay，检查网络拓扑、入口和 provider 对应关系。输出 `passed=true` 只表示静态检查通过；`activation_ready=false` 是有意保留的真实外部门槛。

目标 Mac 获准构建后，先在相同、已审核的本地 Docker host、项目、私密 env 和全部 reviewed/provider overlays 上下文中运行一次性容器的只读 secret/身份探针（Compose 子命令）：

```sh
run --rm --no-deps --user 0:0 gateway python /app/deploy/reviewed_start.py --check-bootstrap
```

它真实读取相应私密文件、执行不可逆降权并打印无凭据的身份结果后退出；不启动网关，不连接数据库或供应商，不修改原始 secret。需要看到 uid/gid10001、四类进程能力均0、no_new_privileges=true 才继续正式 up。静态路径检查或 mock 单测不会代替这个实际权限证明。

本轮执行器为非 root；仅对调用顺序、属主/权限/ctime漂移、恢复 root 失败、降权失败拒绝 exec、能力/NNP 不符拒绝，以及输出不泄密进行了可复跑模拟测试，没有声称本环境实际完成 root setuid/capset。证据见 `provider-secret-bridge-tests.xml` 和 `provider-secret-bridge-verification.json`。

目标 Mac 上获准构建/启动以后，可以用 `exec --user 10001:10001` 在 gateway 容器内运行：

```sh
python /app/deploy/probe_egress.py
```

这个 probe 会检查未授权域、IP 字面值、metadata/IPv6 回环、80 端口、明文 GET 和子域拒绝；对经过审核的主机只建立 CONNECT 隧道，不发送 TLS 业务数据、凭据或推理请求。它不能代替真实账户/模型兼容性测试，也不能证明绕过代理的直接网络路径被阻断。后者必须在 Mac 容器内单独验证直连公共地址失败、经代理仅允许审核域名、数据库不可从主机公网访问。

## 5. 明确剩余的外部阻塞

- Mac 离线，尚无目标 Docker 构建、arm64/amd64 实际运行、read-only 文件系统、network isolation 或 secret file 权限证据。没有为绕开它而启动云端部署。
- 自建 gateway / egress 最终镜像尚未生成，因此最终 digest、完整 OS/npm/Prisma engine SBOM、漏洞扫描和镜像备份还不能产生。当前 Dockerfile 的 APT 依赖和 Prisma 下载仍需构建后记录实际工件。
- 内容 digest 已验证；发布者签名与完整构建 provenance 没有验证。LiteLLM 发布页的 cosign 公钥/签名流程针对上游 GHCR 镜像，不会自动认证本项目的 PyPI 自建镜像。Docker Official Images 也不能凭空套用同一签名策略。
- 真实免费账号资格、供应商条款、隐私范围、实际模型能力交集和用户批准的 secret 文件仍未提供，因此本次不激活任何真实供应商。

混合许可证是上述实际使用范围说明。个人基础功能的官方 OSS 范围已核查，不把笼统的“生产许可待确认”继续列为本项目必须等待的外部门槛；企业功能、镜像再分发或组织特定政策扩展时另行复核。

## 官方资料

- [OCI Distribution manifest 获取](https://github.com/opencontainers/distribution-spec/blob/main/spec.md#pulling-manifests)；[Docker digest](https://docs.docker.com/dhi/core-concepts/digests/)
- [LiteLLM 1.104.0 发布与签名说明](https://github.com/BerriAI/litellm/releases/tag/v1.104.0)；[官方安全公告](https://github.com/BerriAI/litellm/security/advisories)；[OSS/企业功能表](https://docs.litellm.ai/docs/enterprise)
- [Squid 7.7 官方发布](https://github.com/squid-cache/squid/releases/tag/SQUID_7_7)；[Squid 官方安全公告](https://github.com/squid-cache/squid/security/advisories)；[Debian Squid tracker](https://security-tracker.debian.org/tracker/source-package/squid)
- [Squid ACL](https://www.squid-cache.org/Doc/config/acl/)；[http_access](https://www.squid-cache.org/Doc/config/http_access/)；[Docker Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/)
- [Sigstore 本地/离线签名核验](https://docs.sigstore.dev/cosign/verifying/verify/)
