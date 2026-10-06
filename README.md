# 个人免费 AI 网关

为个人 AI 应用提供统一的 API 地址、受限 API Key 和 `general-free` 模型别名。基于 LiteLLM、OpenAI SDK 和 PostgreSQL，按配置顺序调用已审核的免费渠道，支持有限备用和调用诊断。

> 当前版本：`1.0.0rc1`。默认使用模拟上游，真实 Groq / Gemini 渠道尚未启用。免费可用性取决于供应商账户及模型规则，需要自行核查后配置。

## 功能

- **统一接入**：支持 OpenAI 风格的纯文本 Chat Completions 和流式响应。
- **有序备用**：最多两个候选、两次上游尝试，已输出内容后不切换模型。
- **访问控制**：受限应用 Key、渠道授权、超时和并发限制。
- **调用诊断**：记录脱敏状态、耗时及有来源的用量信息。
- **运维支持**：Docker Compose 部署、密钥撤销、备份恢复和配置回滚。

## Docker 快速开始

以下为 Mac 上的本地模拟环境流程。需要 Python 3.12 / 3.13 和已启动的 Docker Desktop（Compose 2.20+）。

1. 按 [部署指南](docs/mac-setup.md#1-准备) 创建 `.env.mock` 和独立私有密钥文件。不要提交这些文件。
2. 在项目根目录执行预检查：

   ```sh
   python3 ops/mac_preflight.py plan
   ```

3. 全部检查通过后，按输出顺序逐条执行启动命令；任一步失败就停止。
4. 按 [应用 Key 配置](docs/mac-setup.md#3-创建一把应用受限-key) 创建受限 Key，再接入应用。

`MANUAL PLAN ONLY` 表示只生成了启动计划，并非报错，也不代表服务已经启动。当前尚无自动完成首次配置的一键部署脚本。

按部署指南使用默认模拟端口后：

| 配置 | 值 |
| --- | --- |
| Base URL | `http://127.0.0.1:4100/v1` |
| 模型 | `general-free` |
| API Key | 创建的受限应用 Key；不要使用管理员 Key |

## 调用示例

创建应用 Key 并保存为 `secrets/local-app.json` 后，运行附带客户端：

```sh
python3 -m venv .client-venv
.client-venv/bin/python -m pip install --require-hashes -r examples/requirements.lock
export GATEWAY_BASE_URL=http://127.0.0.1:4100/v1
.client-venv/bin/python examples/client.py --key-file secrets/local-app.json --prompt '用一句话解释什么是 API。'
```

模拟环境跑通后，按 [渠道接入](docs/provider-onboarding.md) 和 [受控出口配置](docs/controlled-egress.md) 启用真实模型。

## 使用范围

面向个人、低并发、非敏感文本场景：单 worker，并发最多 2。不支持工具调用、多模态、Responses API、多租户或高可用部署；没有内置聊天网页。

已有 Linux 隔离环境回归记录；Mac 完整容器部署和真实供应商调用仍需现场验证。详细结果见 [测试报告](evidence/test-results.md)。

## 文档

- [部署与首次接入](docs/mac-setup.md)
- [API 协议](docs/protocol.md) · [能力矩阵](docs/capability-matrix.md)
- [架构说明](docs/architecture.md)
- [运维与备份恢复](docs/operations-complete.md)
- [诊断与脱敏导出](docs/diagnostics-complete.md)
