# V0.2 发布说明

2026-10-06 UTC。个人免费AI网关的第二个工程候选。**V1尚未完成，真实渠道仍关闭，没有在Mac或Docker上部署。** V0.1原目录、ZIP及历史基线保留；本包是独立新版本。继续复用原生LiteLLM Router/provider/SSE，没有fork、另一套解析器或新前端，依赖不升级。

## 先看两处重要更正

### 1. Mac模拟key操作必须替换旧命令

V0.1的Mac手册指向了默认读取生产.env/4001的key helper，与.env.mock/4101模拟环境不一致，可能失败或选错管理身份。V0.2取消实际执行时的隐式凭据来源：

```sh
python3 ops/key_admin.py create --env-file .env.mock \
  --admin-url http://127.0.0.1:4101 --name mac-mock \
  --deployment-id mock-primary --deployment-id mock-secondary \
  --key-file "$HOME/.gateway-mock-secrets/mac-mock.json"
```

默认仍为dry-run，既不读凭据也不发送请求。所有实际操作，包括info，必须显式提供.env文件。ADMIN_PORT决定回环管理地址；显式URL端口必须匹配。不会回退shell、生产.env或active.env；密码/密钥不输出。完整准备/批准流程见 [Mac手册](mac-setup.md)。生产使用显式 `--env-file .env` 及其匹配端口。

### 2. 流式拒绝仍有未解决的原生缺陷

原生OpenAI/Groq会丢失独立refusal-only分片，包括已有文本之后。此时可能仍输出stop并被记为complete；“空流失败关闭”不覆盖该情形。V0.2没有猜造缺失文本或来源，新增原生Proxy线级/诊断复现并继续关闭生产streaming。

同一非空文本分片附带的refusal可以保留。需要准确拒绝/实测usage时，调用者应明确选择已认证路径的非流式调用。流usage仍unknown，不自动重放或切换协议。已知缺陷复现通过不等于AT-08通过。详见 [原生流审查](../evidence/native-stream-review.md)。

## 新增能力与修复

- 标准库只读Mac预检：OS/架构、Docker client/engine、Compose、私有env、占位值、URL编码密码一致性、端口和配置漂移；全部通过才打印隔离mock手动计划，不执行
- key helper显式目标与私有文件保护：无凭据兜底、端口匹配、禁代理/重定向、符号链接/硬链接/FIFO/大小/权限防护、脱敏错误
- 正常流最终诊断修复：完整响应后到达的ASGI disconnect不再误记取消；提前断开保持取消
- 未知流finish_reason在交付前拒绝；stop/length/content_filter保留已验证语义，不换模型
- 原生Proxy实测完整主备历史与输出上限、两次慢上游共用总时限、连续SSE被总deadline中断且无伪成功
- 失败发布路径：先停可能仍运行的候选，恢复drain，激活本次备份中已校验的旧policy；回退核验通过也保持drain、非零退出、不写成功记录。仅模拟命令测试，Docker实测仍待做

## 恢复验收的旧/新差异

| 范围 | V0.1 | V0.2 |
| --- | --- | --- |
| 同版本备份内容 | 86表/305行一致；活跃key0 | 86表/396行一致；备份时活跃key2 |
| 还原后的原生鉴权 | 未验证 | 有效key可鉴权并实际推理；备份前撤销key仍拒绝 |
| 备份后撤销风险 | 文档警告 | 真实复现过时恢复使key复活；drain期间补撤销、再重启仍拒绝 |
| 轮换/服务重启 | 创建/立即撤销 | 新旧并行→撤销旧key→多个原生进程重启持续拒绝 |
| 数据库中断 | 不可达DSN测试 | 真正停止/启动PostgreSQL；缓存有效key503、零上游 |
| DB回来后的恢复 | 未演练 | 审计丢失后原进程持续失败关闭，明确重启代理并复核才恢复 |
| 配置替换 | validator/计划与stub | 实际原生进程A→B→A，运行revision/真实路由/排空保留均核验 |
| 持久化额度 | 新State重载 | 恢复后零余额和原next_probe保留，耗尽主池零调用，备用正常 |

这些是Linux原生进程/实际PostgreSQL/本地TCP mock证据，不等于Docker发布事务、Mac、跨版本迁移或真实供应商验收。正式恢复仍需完整备份后撤销/权限变更清单；没有新增自动撤销日志系统。

## 测试与边界

- **516项pytest、61个子测试全部通过，0失败、0跳过**；V0.1是416项/19个子测试
- 另行 **8项恢复保护检查**，**189项原生migration** 通过
- 真正pg_dump/pg_restore和原生进程恢复演练通过；临时key撤销、数据库停止、dump/临时目录清理
- pytest有42条已记录上游警告，不声称warning-free
- Mac预检和key helper使用合成系统/stub HTTP；Docker自动回退分支使用模拟命令传输，不能把这些写成目标机运行通过

详见 [测试记录](../evidence/test-results.md)、[独立审查](../evidence/independent-review-v0.2.md)、[原生恢复证据](../evidence/native-recovery.json) 与 [完整AT/FR矩阵](../evidence/acceptance-matrix.md)。

## 仍需完成

Mac/Docker/Nginx、镜像摘要和网络隔离/TLS、容器发布/恢复/失败迁移、真实供应商免费和隐私资格、首个实际消费端、流拒绝/usage完整语义、真实慢读与长时间压力。生产仍无可调用供应商；不把开源文档或mock结果当真实账户证据。
