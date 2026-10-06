# 真实渠道的受控出口

模拟环境不需要本页，也不需要任何供应商 key。先用 [Mac 起步手册](mac-setup.md) 的 SDK mock 跑通基础应用。

本页只说明真实渠道获准后的装配路径，不授权创建/配置持续凭据、接受条款或发真实请求。生产初始没有可用模型，域名清单为 deny-all.invalid。

## 启用前

1. 按 [渠道核查](provider-onboarding.md) 确认具体账户、模型、免费硬边界、隐私和独立池；未核查项继续禁用。
2. 在版本化 policy 填写实际事实、有效复核日期、已测能力与 adapter=openai_sdk；只有已通过资格的目标才能 enabled。
3. 把 `deploy/egress/approved-hosts.txt` 设为这些已启用目标的精确官方域名集合。只允许 api.groq.com / generativelanguage.googleapis.com，不允许通配、额外子域或 IP。
4. 通过获准的私有流程准备只读 provider secret 文件。`GROQ_API_KEY_FILE` / `GEMINI_API_KEY_FILE` 保存文件路径；不要把key值写入policy、命令行、镜像或提交仓库。主机文件/目录权限与Docker Desktop读取能力需要现场验证。

## 配置组合

生产使用 base `compose.yaml`、明确 `compose.egress.yaml`，再只选择实际批准 provider 的 `compose.groq.yaml` / `compose.gemini.yaml`。维护CLI必须使用同一显式组合，详见 [运维手册](operations-complete.md)，不能手工base-only重建后丢掉出口和secret。

gateway/PostgreSQL仍只在internal网络；只有Squid同时连接出口网络。Squid没有宿主端口，不持有任何供应商/数据库key，只有精确443 CONNECT；私网、回环、metadata、特殊地址和其他方法被拒绝，不解密TLS、不安装CA、不保存请求access log。

`reviewed_start.py` 会清除继承的provider key和代理变量，固定受控代理地址，只加载当前已启用provider的只读secret。缺secret、域名与启用目标不一致或资格过期都会拒绝启动，不回退shell已有key。

## 验证

源码只读检查：

```sh
python deploy/check_reviewed.py
python tools/supplychain_verify.py
```

`passed=true` 只说明静态/文件完整性。`activation_ready=false` 明确表示尚未替你完成真实现场验收。

获准在目标Mac构建/启动后，在相同gateway容器内执行 `python /app/deploy/probe_egress.py`。它检查未授权域、IP、metadata、IPv6回环、非443、明文GET和子域拒绝；对已批准主机仅建立CONNECT隧道，不发送推理内容或凭据。

还必须在容器内确认绕过代理直连公网失败、实际SDK确实走代理、DNS/IPv4/IPv6和重定向约束、入口仅loopback、PG没有公网暴露。CONNECT通过不是账号资格或模型兼容性验证。

固定官方Squid7.7源码在隔离Linux编译，并完成19项真实TCP/ACL验证，其中“已批准域解析到回环”仍拒绝；它不是Mac容器网络证明。基础镜像摘要、许可证与公告、最终镜像仍缺的材料见 [供应链记录](../evidence/supply-chain-complete.md)。
