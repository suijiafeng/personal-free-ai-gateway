# 原生请求并发槽清理补齐

核查日期：2026-10-06。仅使用合成正文、合成 key、loopback 上游和临时 PostgreSQL。

## 真实问题

同一把 max_parallel_requests=2 的原生 key 连续进行“非流式总 deadline → 正常请求”：

1. 预热成功，原生槽数 0。
2. 第一次 504 后，网关和上游活动请求都为 0，原生槽数却为 1；正常请求仍成功，但该残留未释放。
3. 第二次 504 后原生槽数为 2；之后正常请求持续返回 429，上游没有新调用。

全过程没有清缓存、修改计数或换 key。原生槽 TTL 为 3600 秒，不能把这个泄漏当作暂时的正常限流。只读观测见 native-slot-leak-before.json。

## 原因与最小修复

锁定版本的原生流取消清理会调用 async_release_max_parallel_requests_on_disconnect，但入口的非流式总 deadline 可能取消整个调用，跳过原生成功/失败回调，留下预扣的槽。

当前薄入口在每个实际进入原生 app 的生成请求 finally 中补调用该现有命名方法；输入已在外层被拒绝的请求没有原生预扣，因此不会调用此清理：

- 这是针对 LiteLLM 1.104.0 的版本绑定生命周期集成，不能宣传为通用插件文档保证。
- bootstrap 校验版本、handler 的精确类型和方法可调用性。未知类型、缺失方法或版本不符不静默绕过限制。
- 原生实现用当前请求上下文里的 slot_id 和所属 counter_keys 释放；内部有 parallel_slot_release_lock，已释放时幂等返回。没有在生产代码读取、减一或清空私有计数器。
- 传入的空 UserAPIKeyAuth 不含凭据；本 pin 的该方法依赖原生 request stash 确定归属，不能通过传入另一把 key 来释放其他请求。
- 清理独立使用 1 秒上限及 shield；异常只记固定脱敏错误，并把状态标为不健康，阻止后续请求。原有元数据清理仍执行，本地全局并发占用仍在 finally 释放。
- 保留全局并发 2 和原生每 key 的 1/2 限制，没有删除配置、换 key 或把用户要求悄悄降级。

版本化来源：[原生 v3 limiter](https://github.com/BerriAI/litellm/blob/v1.104.0/litellm/proxy/hooks/parallel_request_limiter_v3.py) 的 async_release_max_parallel_requests_on_disconnect、_release_stashed_parallel_slot 与 _release_parallel_request_slots；[原生流清理调用处](https://github.com/BerriAI/litellm/blob/v1.104.0/litellm/proxy/common_request_processing.py)。

## 覆盖范围

- 同一个 key 连续三轮非流式 deadline、流式 deadline、客户端取消和传输失败后均可正常继续。
- 原生已预扣、尚未进入 deployment hook 时取消，也不积累占用；上游调用数为零。
- 两个真实活动请求中取消一个，只释放它自己的占用；另一个仍活动，新请求最多填补一个位置，全局第三个仍被拒绝。
- max_parallel_requests=1 的同一 key，在全局尚有空位时连续拒绝其他请求。保持 HTTPX 读取迭代器引用，确保被测流没有因为测试端垃圾回收提前关闭。此场景重复完整三轮，验证正常及取消后的 native/补充清理不会放掉别人的槽。
- 清理函数挂起或抛异常时，有界结束、固定脱敏日志、健康检查失败，下一请求不进入上游；不伪造审计成功记录。
- 备份恢复出的原 key 仍保留 max_parallel_requests=2，使用相同 key 进行三轮真实总 deadline 后正常调用；没有重新生成 key 或清其原生状态。此项结果保存在原生恢复报告中。

SDK Proxy 定向结果、JUnit 与终端输出保存在 sdk-proxy-postgres-*；故障清理单元见 native-slot-cleanup-unit-*；完整最终结论以 canonical run 和 native-recovery.json 为准；operations-native-recovery.json 保留此前独立预检。只有测试夹具做合成预热，生产启动不会额外调用供应商。

## 收尾结果

- SDK Proxy/真实临时PG核心加生命周期41项、10个subtests通过；key1完整三轮加严又单独通过。
- 备份恢复旧key2的三轮实际deadline→成功已通过，原max_parallel_requests仍为2，见operations-native-recovery.json。
- 最后native_entered范围与非法请求清理同步变更：8项清理/背压单元及3项原生PG定向通过。非法输入仍逐项400、未知别名404、实际上游调用为零；各独立输入之间等待必要审计清理完成，没有重试该输入、清状态或改变断言。
- 原生PG定向保留了Router协程析构时KeyError '__import__'的依赖warning。未修改vendor或删除警告来宣称通过。
- 最终 canonical 已通过：789 个主测试、71 个子测试、8 项另行恢复守卫和完整原生恢复均通过；同 key 恢复证明见 native-recovery.json。定向结果不与整仓重复相加。
