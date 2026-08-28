# P6-04 高置信反馈证据边界

## 结论

已修复旧反馈链路的主体污染：其他群友在机器人回复后相邻发送“哈哈”“不对”“好可爱”等文本，不再自动写入被回复用户的个人画像，也不再直接调整表达权重。

高置信反馈现在必须绑定一个精确的内部 reply ID，并满足以下至少一种来源：

1. 被回复的目标本人在有效观察窗口内明确表达正/负反馈；
2. 新消息通过平台结构化 `reply_to_message_id` 精确引用该回复的已知平台 message ID；
3. 平台 reaction 事件精确指向该回复的已知平台 message ID。

## typed 证据

新增 `FeedbackEvidence`：

- `reviewer_key`：真实发送者 principal；
- `reply_id`：精确内部回复 ID；
- `signal`：positive / negative；
- `source`：target follow-up / explicit reference / platform reaction / adjacent group message；
- `confidence`：high / low；
- `is_target_user` 和 `affects_target_profile`：明确区分“相关反馈”和“可写个人画像”。

trace 只输出来源、置信度和布尔状态，不输出 reviewer、reply 或平台 message ID。

## 个人画像规则

- 只有 `is_target_user=true` 且证据为 high，才能修改该目标用户的正负反馈计数。
- 其他用户即便精确引用/回应机器人，也不能修改目标用户画像。
- 普通相邻群消息是 low，本层直接忽略，不修改个人画像或表达权重。
- 没有内部 reply ID 的旧观察不能升级为 high。

## 平台 ID 边界

- AstrBot 当前通用发送路径没有平台 message ID，因此普通生产回复暂时主要依赖“目标本人 follow-up”形成高置信反馈。
- 精确引用/reaction 路径已经实现，但只有未来获得明确 typed 平台 ID 后才会生效。
- 不根据时间、正文、昵称或日志猜平台 ID。

## 接入

- `AmbientMessage` 增加结构化 inbound `message_id` 和 `reply_to_message_id`。
- `ingest_ambient_event()` 从已验证 `TurnEnvelope` 传递这些字段，不从文本解析引用关系。
- `ReplyObservation` 保存内部 reply ID 和已确认的平台 message ID 集合。
- 新增 `observe_platform_reaction()` typed 入口；未精确命中已知平台 ID时只能得到 low。

## 验证

回归覆盖：

- 目标本人 follow-up 为 high 并更新其画像；
- 其他群友相邻“哈哈”不更新目标画像，也不改变表达权重；
- 其他群友精确引用为 high relevance，但不污染目标画像；
- 精确平台 reaction 可形成 high；
- 引用 ID 不匹配或缺少内部 reply ID时不能升级；
- evidence trace 不泄漏身份和 reply ID。

完整测试：426 项执行，423 通过，3 项既有 expected failure。`git diff --check` 通过。

## 边界

- 本层对 low 群体反馈只分类、不学习；P6-05 才建立独立群体信号。
- 本层没有部署，没有修改 AstrBot、LivingMemory 或第三方插件，没有 Git 写操作。

## 下一层

P6-05 将把其他群友的低置信反馈收集到独立、衰减、有界的群体氛围信号中；它只能辅助表达排序，不能改变任何个人关系或主人/群友身份。
