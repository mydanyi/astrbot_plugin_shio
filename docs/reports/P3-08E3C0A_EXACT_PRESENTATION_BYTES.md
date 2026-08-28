# P3-08E3C0A：最终展示字节与语义 Seal 闭合

日期：2026-08-18（Asia/Hong_Kong）

## 交付结论

本层关闭了 P3-07 在 E3C 审计中暴露的两个发送边界：

1. `SemanticGuardController.consume_final()` 现在只接实际最终正文并在 Controller 内部重新执行 FINAL_SEND 校验；其正文摘要、INITIAL/REPAIR 已签发摘要和 `PresentationHandoff` 摘要必须完全一致。语义仍然正确但被追加、删减或同义改写的文本也不能复用旧 seal。
2. `PresentationHandoff.final_visible_text` 与 `final_text_digest` 现在来自同一份完整正文，不再保存前 1000 字却对全文取摘要。

同时收紧了 `PresentationHandoff.__repr__`，不再回显正文或目标消息 ID。

第一次修复后独立审计又复现了 public `SemanticGuardReport(FINAL_SEND, old_digest, ())` 伪造：旧 `consume_final(report=...)` 会信任公开数据形状。现已从 `issue`/`consume_final` 权威 API 中物理移除 `report` 参数；INITIAL/REPAIR 和 FINAL_SEND 都由 Controller 根据实际正文自行重验，公开 report 只保留为诊断/repair 数据，不能签发或消费 seal。

## 先红后绿

先新增四个稳定红灯：

- FINAL_SEND 同义改写后仍错误消费旧 seal；
- FINAL_SEND 追加内容后仍错误消费旧 seal；
- FINAL_SEND 截断标点后仍错误消费旧 seal；
- 超过 1000 字的展示正文与 handoff 保存正文不一致。

修复后新增生产集成回归，证明 dispatch 阶段即使变化后的文本仍通过语义判断，也会因 exact-byte seal 不一致而清链、零发送。

原先依靠发送前清理协议文本后继续发送的测试已迁移为 fail closed：发送前若仍需清理 meme/tool 协议，清理后的文本不再冒用旧 Presentation seal，而是整条阻断。正常气泡测试改为使用 `guard_persona_reply()` 实际产出的同一规范化文本，避免测试伪造 post-presentation 变更。

验证结果：

- SemanticGuard + PresentationHandoff + OutputValidator + RepairController + Pipeline：131/131；
- `compileall`：通过；
- 限定 `git diff --check`：通过。

二次独立终审 `blocker=0`：public report 向 `issue`/`consume_final` 注入均因 API 无该参数而拒绝；同义改写/追加/截断、1840 字完整正文、普通气泡、hidden/cleaned Python/XML protocol、repr/trace 边界全部复跑通过，审计前后目标文件哈希未变化。

完整全量暂不在本报告中宣称通过：E3C0 同期正在原子迁移 Router/Controller 接口，稳定冻结后统一复跑。

## 不变量

```text
FINAL_SEND report.visible_digest
== issued SemanticValidationSeal guarded_digest
== PresentationHandoff.final_text_digest
== 实际聚合待发送文本摘要
```

- 任一文本字节变化都使 seal 失效；
- public/伪造/copy 的 `SemanticGuardReport` 不属于授权输入；
- seal 仍是 exact contract + exact presentation object 的一次性消费；
- 协议清理、气泡拆分或其他 presentation 处理不得静默生成第二份已授权正文；
- 本层不执行 owner action、不接 `main.py` 的新动作分支、不改配置、不触碰 FNOS、不执行 Git/GitHub 写操作。
