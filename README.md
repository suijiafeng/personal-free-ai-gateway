# 个人免费 AI 网关

给自用的基础 AI 应用提供一个固定地址、一把受限调用 key 和 `general-free` 模型别名。先筛选已批准的免费渠道，再按人工顺位调用；不可用时有限备用，没有合格目标就明确失败。

本目录是完整工程候选 `1.0.0rc1`，不是已在你的 Mac 上线。真实 Groq/Gemini 候选仍全部停用，没有真实凭据、付费备用或默认可用的免费模型。V0.1/V0.2 原源码与交付保持不变。

一页交接见 [先看这里](START-HERE.md)。

## 最短上手路径

1. 按 [Mac 起步手册](docs/mac-setup.md) 准备私有配置，执行 `python3 ops/mac_preflight.py plan`。它只检查和给出本地 mock 的启动计划，不安装软件、创建凭据或启服务。
2. 启动隔离的模拟栈后，创建受限应用 key。应用使用 `http://127.0.0.1:4100/v1`、`general-free` 和这把 key。管理员密钥不能用于生成。
3. 用 [官方 SDK 示例](examples/client.py) 验证，再在你的小应用里复用。该示例已经实际穿过原生 Proxy、PostgreSQL 与模拟上游验证，包含非流式、流式、错误和取消；不要求你另选 IDE。

客户端依赖独立于服务端，只需 16 个已锁定包：

```sh
python3 -m venv .client-venv
.client-venv/bin/python -m pip install --require-hashes -r examples/requirements.lock
export GATEWAY_BASE_URL=http://127.0.0.1:4100/v1
.client-venv/bin/python examples/client.py --key-file secrets/local-app.json --prompt '用一句话解释什么是 API。'
```

真实模型要在 mock 跑通后按 [渠道核查](docs/provider-onboarding.md) 与 [受控出口](docs/controlled-egress.md) 启用。模型名或公开“免费”文档不能代替你的账户/隐私/费用硬边界核查。

## 已实现的核心

- 固定 LiteLLM 1.104.0、官方 OpenAI SDK 2.54.0、PostgreSQL；不 fork、不自写 SSE、不建第二套路由
- 纯文本 Chat Completions 与受限模型列表；输入白名单、主备能力交集、每次实际调用重新检查免费/权限/隐私/目标
- 最多两个候选、两次上游尝试；单 worker、并发 2；总时限、取消、慢读释放和共享池受控探测
- 推荐 SDK 窄适配保留独立拒绝分片、文字后拒绝、合法空流及真实终态；发生中断不伪造完成，不在已输出后换模型
- 用量/余额有来源和时效，缺失保持未知；不把原生估算或 SDK 类型转换当实测，不造“账单已确认零费用”
- 历史候选决策、全部尝试、首事件/总耗时、重试/冷却与终态；管理员筛选分页与私有脱敏导出
- 原生受限 key、撤销账本、发布/回滚/备份恢复护栏；旧备份恢复后先核对撤销，保持排空直到验证
- 数据库运行/迁移/备份/初始化身份分离；固定基础镜像摘要、受控 CONNECT 出口、文件式 provider secrets

完整能力边界见 [能力矩阵](docs/capability-matrix.md)，输入/响应见 [协议](docs/protocol.md)。工具调用、JSON Schema、多模态、Responses、多租户、计费、HA 和新网页后台不在原规格首版必做范围。

## 流式与用量的精确边界

默认模拟入口使用 `config/policy.sdk.mock.yaml`。推荐路径是官方 CustomLLM 扩展连接已锁定官方 SDK，SDK负责HTTP/JSON/SSE，原生网关负责路由和公共输出。独立拒绝丢失的旧原生路径仍保留回归复现，生产配置禁止 `native + streaming`。

SDK 来源经过严格原始对象校验后，可将真实流用量写入最终私有诊断；公共流不开放 `stream_options` 或承诺最终 usage 块。未知、部分或非法用量保持 null/unknown。见 [适配决策](docs/adr-sdk-stream-bridge.md) 与 [来源说明](docs/usage-provenance.md)。

## 复现工程验证

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
export LITELLM_LOCAL_MODEL_COST_MAP=True
.venv/bin/python -m gateway.config validate --config config/policy.yaml
.venv/bin/python -m gateway.config validate --config config/policy.sdk.mock.yaml
.venv/bin/python -m pytest tests ops -q
```

上述直接pytest命令用于没有设置 `GATEWAY_TEST_DATABASE_URL` 的基础检查；数据库用例会明确skip。带数据库的完整验收必须使用下面的runner，它为两适配器各启动新的Python进程，避免原生全局状态复用；不能在同一进程重复构造网关。

未提供一次性 PostgreSQL 时，数据库测试明确 skip，不能写成通过。完整 runner 建立新的 loopback UTF8 临时库，不接受既有数据库：

```sh
.venv/bin/python tests/run_postgres_suite.py \
  --postgres-bin /path/to/postgresql/17/bin
```

它运行原生189项 migration、扩展schema、完整回归及真实进程/数据库恢复，最后停止并删除自己创建的临时实例。role/SCRAM、Squid组件和供应链各有独立证据，不以单元测试总数冒充产品验收。最终执行记录见 [测试报告](evidence/test-results.md)、[独立复核](evidence/original-spec-independent-audit.md) 和 [AT/FR逐项矩阵](evidence/acceptance-matrix.md)。

## 现在还需要现场完成什么

- 连接你选定的 Mac，核对 Docker/Compose、芯片架构、镜像构建/最终SBOM与公告、Nginx入口、网络隔离、secret权限和容器发布/恢复
- 安全地准备你授权的私有凭据，核查真实免费账户/模型/隐私/独立额度池，然后进行最少真实调用

隔离 Linux 进程、数据库和 Squid 的真实测试不能替代上述 Mac 组合验收。没有切换到别的机器替你部署，没有擅自创建真实账户/持久供应商凭据或发送真实内容。

## 维护入口

- [Mac 起步](docs/mac-setup.md)
- [运维、撤销、发布、恢复与备份清理](docs/operations-complete.md)
- [数据库最小权限](docs/database-roles.md)
- [诊断与脱敏导出](docs/diagnostics-complete.md)
- [供应链与出口证据](evidence/supply-chain-complete.md)
- [架构](docs/architecture.md)、[原始规格](docs/source-spec.md)

所有维护 CLI 默认预览；实际执行需明确目标、私有配置及操作确认。不使用 `.env`/shell 的隐式凭据备用，不把恢复失败或缺失证据显示成成功。
