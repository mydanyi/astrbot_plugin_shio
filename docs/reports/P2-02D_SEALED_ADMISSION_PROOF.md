# P2-02D Sealed AdmissionProof 合同补口报告

## 结论

P2-02D 已把 verified gate 与 `ACCEPT_HUMAN` 的来源证明收回同一个 `IngressAdmissionController`。公开 module issuer 不能签发 VERIFIED observation；只有 controller 能针对 exact `IngressEvent` 绑定 issuer seal、完整 candidate binding 和 content digest。controller 校验并一次性 claim 该 observation、成功提交 `ConversationRevisionBook` 并得到 canonical `ConversationEvent` 后，才会签发 opaque `AdmissionProof`。公开字段自洽的 `AdmissionResult`、复制出的 proof、跨 admission、跨 controller、wrong-event 与 proof/gate replay 都不能通过 canonical claim。

本补口只修改 Ingress/Affect 纯合同与测试，不接 `main.py`，不改变生产参与、规划或发送行为。

## 先红后绿

- 先在 `tests/test_ingress_admission.py` 增加 proof issuer、公开伪造、跨 controller、跨 admission、gate replay、proof replay 与隐私用例；
- 第一次定向运行稳定红灯：`ImportError: cannot import name 'AdmissionProof'`；
- 第一版实现后 IngressAdmission + AffectState 定向测试 `24/24` 通过；
- 独立安全复核随后用 wrong-event digest 复现出错误准入；新增“公开 VERIFIED issuer 拒绝、wrong-event 零 revision/零 claim、原事件仍可使用同 observation”红测后，旧实现稳定失败为 `ContractViolation not raised`；
- 把 VERIFIED issuer 收回 controller 并完成 exact binding 后，IngressAdmission + AffectState 定向测试 `25/25` 通过；
- IngressAdmission、AffectState、MemoryPolicy、ConversationEvent、GroupScene 相关回归 `54/54` 通过；
- 排除仍在并发迁移的旧 `test_pipeline.py` 后，完整非 pipeline 测试 `564/564` 通过。
- typed-only pipeline 迁移稳定后最终全量测试 `628/628` 通过。

## Controller 颁发边界

`AdmissionProof` 为 `frozen=True, slots=True, init=False`，不能用公开构造器创建。它只在以下顺序全部成功后由 controller 内部生成：

1. 输入必须是 typed `IngressEvent`；
2. VERIFIED `GateObservation` 必须由当前 controller 针对 exact `IngressEvent` 签发，携带当前 controller issuer、完整 candidate binding digest 与 `event.content_digest`；公开 module issuer 对 VERIFIED 固定拒绝；
3. observation 必须具有 ingress 模块私有 seal，controller/binding/content 必须逐项匹配，并且此前未被 claim；
4. disposition 只能由 typed sender kind 与 gate verdict 决定；
5. `ConversationRevisionBook.commit()` 必须返回与 decision 同 binding 的 canonical `ConversationEvent`；
6. controller 才生成 proof，并把 proof 对象与原始 `AdmissionResult` 对象成对注册到该 controller 私有 registry。

Drop、ban、self、known bot、plugin echo、unknown/degraded 结果没有 `ConversationEvent`，也不携带 proof。

## Proof 绑定内容

Proof 内部绑定：

- 完整 `DecisionBinding` 的 SHA-256；
- canonical `IngressEvent` 结构与 content digest 的 SHA-256；
- sealed gate 的 status、ban verdict 与 source-event digest 的 SHA-256；
- committed conversation revision；
- controller 私有 nonce、上述摘要与 revision 形成的 commit digest；
- 当前 controller 独有的 issuer token。

验证时还要求 revision book 当前 revision 不低于 proof 的 committed revision。摘要只用于绑定，不替代 controller 的对象身份 registry。

## Canonical、跨 Controller 与一次性消费

`inspect_admission_proof()` 与 `claim_admission_proof()` 都要求：

- proof seal 正确；
- issuer token 与当前 controller 完全相同；
- registry 中的 proof 必须是同一个对象；
- registry 中的 `AdmissionResult` 也必须是调用者传入的同一个 canonical 对象；
- binding/ingress/gate/commit/revision 摘要重新计算仍一致。

因此 `dataclasses.replace(result)` 生成的字段完全相同副本也会被拒绝。即使把合法 proof 的全部字段和隐藏 seal 复制到新对象，registry object identity 仍会拒绝。另一 controller 无法 inspect/claim 本 controller 的 proof。`claim_admission_proof()` 原子消费一次；第二次 inspect 或 claim 固定报 replay。

公开 `issue_gate_observation()` 只可创建 fail-closed degraded observation；请求 VERIFIED 固定报 `verified_gate_controller_required`。`IngressAdmissionController.issue_gate_observation()` 才能签 exact-event observation。`admit()` 的顺序是 binding verify → one-time claim → revision commit → proof issue：wrong-event 在 claim/commit 前失败，revision 保持 0，observation 仍可回到它绑定的原事件；同事件第二次使用固定按 replay 拒绝，跨 controller 按 issuer mismatch 拒绝。

## AffectState 消费方式

`issue_affect_admission_evidence()` 不再根据公开 `AdmissionResult`/`GateObservation` 字段自行签发。它必须拿到原 controller，并先由 controller canonical inspect；公开复制、跨 controller、drop/degraded 与已消费结果均失败。

`AffectStateBook` 显式绑定同一个 long-lived `IngressAdmissionController`。状态写入前先 inspect，完成 revision、clock、source 与 appraisal 硬门后，再在提交状态前原子 claim proof。claim 失败时 AffectState、scope revision 与 target ledger 均尚未写入。成功 proof 不能在同一或另一 AffectStateBook 重放。

## 隐私与容量

`AdmissionResult` 的 ingress、gate evidence、decision、conversation event 与 proof 均为 `repr=False`；`AdmissionProof` 的所有摘要和 issuer 数据也为 `repr=False`。trace 只报告闭集状态、绑定布尔和 revision，不回显正文、raw scope/session/message/sender ID、trace ID、content digest、gate 参数或 proof 摘要。

每个 controller 的 active/consumed proof registry 均有显式上限，防止未消费 proof 无限增长；淘汰后的旧 proof 失败关闭。

## 生产接线复核

本子任务没有自行修改 `main.py`。集成所有者随后原子把 `_reneban_arrival_gate(event, ingress_event)` 改为无论 verified/degraded 都调用同一个 `self.ingress_admission.issue_gate_observation(ingress_event, ...)`，并移除公开 issuer import。只读复核确认生产入口现在传入创建 admission 的 exact `IngressEvent` 与同一 controller，不存在临时 controller 或摘要字符串旁路。

## 边界

- 这是一套进程内 capability/identity 合同，不宣称抵抗能任意改写 Python 进程内存的攻击者；
- GateObservation 的真实 AstrBot/ReNeBan hook 来源仍由既有 adapter 与生产 hook 顺序负责；本合同把“持有该 long-lived controller 实例”定义为进程内 trusted issuer capability；
- 本任务未部署、未修改计划或第三方插件，未执行 Git/GitHub 写操作；旧 `test_pipeline.py` 的 typed-only 迁移由并发任务统一收口，迁移稳定后最终全量已通过。
