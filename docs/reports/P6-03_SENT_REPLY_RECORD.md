# P6-03 正式 SentReplyRecord

## 结论

星汐现在可以用内部 reply ID 精确查询一轮回复的真实发送记录。记录包含准确目标、所有真实发送尝试、成功/失败状态、群内实际可见文本、表达来源以及可选平台消息 ID；没有按时间、正文相似度或日志进行模糊关联的入口。

## 数据结构

新增不可变 `SentReplyRecord`：

| 字段 | 含义 |
| --- | --- |
| `reply_id` | 星汐内部唯一回复 ID，不冒充平台 ID |
| target 字段 | 目标 message、sender principal、session、scope、引用边和输入摘要 |
| `segments` | 实际计划/尝试过的每个发送分段 |
| `expression_ids` | 本轮实际采用的表达候选来源，最多 3 个 |
| `created_at` | 内部回复建立时间 |
| `sent_at` | 最后一个成功 segment 的明确完成时间；没有成功则为 0 |

每个 segment 保存：

- 真实可见文本及其摘要、长度；
- `planned / attempted / succeeded / failed` 状态；
- 尝试和完成时间；
- 可选平台 message ID 及 typed 回执来源；
- 非正文失败类型。

## 精确性规则

- `successful_segments` 只返回明确成功的真实 segment。
- `successful_visible_text` 按实际成功顺序重建群内可见内容。
- 失败后改为合并发送时，同时保留失败尝试和真实成功 fallback，不篡改历史。
- 查询只接受内部 reply ID。
- 平台 ID 仍必须来自明确 typed 回执；当前 AstrBot 4.27.2 通用路径保持为空。

## 隐私和资源边界

- 原始可见文本仅保存在进程内的有界运行态账本，不写入仓库或聚合状态文件。
- trace 只输出状态计数、长度和布尔能力，不输出正文、真实用户 ID、目标 message ID 或内部 reply ID。
- 账本默认最多保留 512 轮回复；容量满时优先淘汰已终结记录，并同步删除 segment 索引，避免长期群聊导致无界内存增长。

## 验证

新增回归覆盖：

- 精确目标、引用边和表达来源；
- 真实成功文本和失败 segment 同时保留；
- trace 不包含正文或身份值；
- 有界容量及索引同步淘汰；
- 生产多气泡路径可重建实际成功的三条消息。

完整测试：416 项执行，413 通过，3 项既有 expected failure。`git diff --check` 通过。

## 边界

- 本层没有持久化原始群聊或回复正文。
- 本层没有实现反馈置信度判定；这是 P6-04/P6-05。
- 本层没有部署，没有修改第三方插件，没有 Git 写操作。

## 下一层

P6-04 将建立 typed `FeedbackEvidence`：只有目标本人、明确引用本回复或平台 reaction 事件可以形成高置信证据；普通相邻群消息不能高置信修改目标用户画像或表达权重。
