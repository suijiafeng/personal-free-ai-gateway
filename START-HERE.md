# 先看这里

这是给个人基础 AI 应用使用的完整工程候选 `1.0.0rc1`。核心代码与隔离实测已完成；尚未部署到你的 Mac，真实模型渠道默认全部关闭。

## 第一次使用

1. 解压后进入本目录，按 [Mac 起步手册](docs/mac-setup.md) 准备本机 Docker 和私有配置。秘密值只保存在你自己的私有文件，不发送到聊天。
2. 在项目根运行 `python3 ops/mac_preflight.py plan`。它只读检查，全部通过才列出七步模拟栈启动计划；审阅后逐条执行。
3. 按同一手册创建一把受限应用 key。参考应用直接用 `--key-file secrets/local-app.json` 读取，不必把 key 粘贴到命令行。
4. 应用接入地址 `http://127.0.0.1:4100/v1`，模型 `general-free`。运行 [README 中的示例命令](README.md)，先验证普通文本、流式和错误。
5. 模拟路径成功后，再按 [渠道核查](docs/provider-onboarding.md) 与 [受控出口](docs/controlled-egress.md) 审核并启用真实免费渠道。

基础应用不需要另一个网页后台或商业 IDE。生产凭据创建、系统配置和真实调用仍由你批准具体操作后执行。

## 已核验到什么程度

- 完整回归：789 个主测试、71 个子测试及 8 项恢复守卫通过，0 失败/错误/跳过；依赖警告如实保留
- 实际 Linux 原生 Proxy/PostgreSQL、参考 SDK 应用、同 key 连续超时后继续使用、真实备份/恢复与撤销重放均通过
- 独立数据库最小权限和 Squid 编译/ACL 组件通过
- 尚需 Mac 的 Docker/Compose/Nginx/secret 跨 UID/网络组合、最终镜像供应链，以及真实免费账户和模型调用现场核验

详见 [最终测试报告](evidence/test-results.md)、[逐条验收矩阵](evidence/acceptance-matrix.md)。维护、停止、备份和恢复使用 [运维手册](docs/operations-complete.md)；不要删除卷、放宽 secret 权限或绕过失败关闭来解决启动问题。
