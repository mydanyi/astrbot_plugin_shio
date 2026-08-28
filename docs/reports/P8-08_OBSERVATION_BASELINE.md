# P8-08-OBS 生产观察基线报告

## 结论

P8-08-OBS 已开始，但尚未达到任何放量或改码阈值。首轮只读窗口为 `2026-08-17T14:24:59Z` 至 `2026-08-17T14:30:15Z`：没有真实主人私聊进入 typed 回复链，当前有效观察进度为 `0/20`。

因此本轮不修改代码、不调整 Prompt、不改变 rollout scope，也不把“没有看到错误”误写成 owner/private 生产验收通过。

## 只读健康状态

- 容器：running=true、restarting=false、OOMKilled=false、dead=false、RestartCount=0。
- StartedAt：`2026-08-17T14:24:59.108734467Z`，与 P8-08 重载记录一致。
- WebUI：HTTP 200，响应长度 4128。
- 日志窗口：195 行，首条 `2026-08-17T14:25:01.872682603Z`，末条 `2026-08-17T14:27:37.428555125Z`。
- Traceback：0。
- ERROR token：0。
- `<|channel|>` 协议候选：0。
- 裸 `search_memes{...}` 候选：0。

日志只做脱敏计数，没有把原始群聊、真实 sender/group ID、Prompt、Token 或完整模型输入输出写入本报告。

## typed 观察计数

| 指标 | 计数 |
| --- | ---: |
| `typed_reply.prepared` | 0 |
| 唯一 typed trace | 0 |
| typed trace 对应 `send.succeeded` | 0 |
| `typed_reply.blocked` | 0 |
| `typed_reply.repair_rejected` | 0 |
| `typed_reply.state_missing` | 0 |
| 脱敏 `ambient.decision` | 4 |

`typed_reply.prepared` 是生产 active 分支无条件写出的脱敏结构化日志，因此该计数为 0 表示窗口内没有满足可信主人、私聊和简单纯文本条件的真实轮次；不能用 4 条 ambient 判定代替 owner/private 样本。

## 修改阈值判断

- 真实 owner/private typed 样本：不足，`0/20`。
- P0/P1 回归：本窗口未观测到，但因为没有 active 样本，这只证明启动后基线健康，不证明 typed 生产回答已验收。
- 是否修改代码：否。
- 是否扩大到群聊：否。
- 是否允许 P8-09 删除旧链路：否。

下一轮仍从相同 StartedAt 之后增量读取日志，按唯一 trace 统计 active 样本，并关联 prepared、Validator、repair/block、最终发送和异常。累计至少 20 个真实主人私聊 typed 轮次且 0 个 P0/P1 回归后，才进入显式群范围 rollout 设计。

## 发布边界

本轮没有修改线上文件或配置，没有重启容器，也没有执行 `git add`、`git commit`、`git push`、PR、合并或 Release。
