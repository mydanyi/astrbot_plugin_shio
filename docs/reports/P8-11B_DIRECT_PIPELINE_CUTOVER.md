# P8-11B 全部直接对话 typed 接管

## 根因

此前 `owner_private` 门只接受主人私聊简单纯文本；复杂内容、群聊、群友、引用、联网和多模态会把 `enabled` 降回 shadow，随后继续执行旧 `SpeechPlanner → Replyer → 通用口语化守卫`。因此“已经启用新版”只对很窄范围成立。

## 修改

- 增加 `architecture_v2_rollout_scope=all`，只接受 AstrBot 当前事件提供的可信结构化 sender 和完整 ReplyTarget。
- 增加统一 `build_direct_chat_plan`：复杂度、引用和附件只改变回复形态/输入，不再选择另一套 Planner。
- owner/guest、group/private 共用 PersonaPackage、Affect、ExpressionCandidate、ReplyComposer 和 OutputValidator v2。
- `/task`/`/agent` 不再绕过星汐；主人完整工具仍在 typed 身份和输出边界中运行。
- typed 准备失败会清空请求并停止事件，禁止交给旧链或原始人格继续生成。

## 验证

入口矩阵通过；所有 new-only 直接用例的旧 Planner Provider 调用为 0。
