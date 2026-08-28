# P4-03 Persona Renderer 报告

## 结论

P4-03 已完成本地与隔离 AstrBot Linux container 门，候选未部署。

最终 Persona Renderer 现在必须接收一份 exact `AffectRenderContext`。它由 long-lived `AffectStateBook` 对当前 exact `ACCEPTED_HUMAN` mutation、当前仍在 book 中的 state 和当前 exact `DecisionBinding` 签发。Renderer 只能看到脱敏的 cause、valence、arousal、intensity、inertia、version 与 carryover flag，不能获得 sender、scope、message、trace 或任何 authority。

同一当前消息在 ATRI 与替代 Persona 下继续共享相同的 `ContentIntent`、ReplyTarget、CapabilityPolicy、system renderer contract 和 `[当前轮语义与证据]`；差异只存在于 `[人格与表达]`。

## 根因

P4-01 已将跨轮 AffectState 接入 accepted-turn 与真实 send receipt，但 P4-02 冻结热路径仍在最终渲染前重新计算一次 stateless `AffectAppraisal`。这能决定当前触发，却无法表达上一轮留下、随时间衰减并在真实发送后平复的连续状态。

旧 Composer 同时没有 required continuous-state 字段，因此任何内部调用者都能构造不带连续状态的 canonical request。

## 实现

### `core/affect_state.py`

- 新增 constructor-disabled、copy 不可认证的 `AffectRenderContext`；
- context vault 绑定 exact context identity、完整字段快照、exact binding identity/快照、源 state identity/快照和源 `AffectStateBook`；
- `AffectStateBook.issue_render_context()` 只接受 exact `ACCEPTED_HUMAN` mutation；
- 源 state 必须仍是当前 `(scope, sender)` state，revision 必须等于当前 binding；
- state、context 或 binding 的字段删除、等值副本、原地修改、跨轮替换全部失败关闭；
- context vault 使用 weak reference 和固定 512 容量；
- 仅测试夹具使用独立 synthetic test issuer，生产路径仍必须经过 book 当前态检查。

### `core/reply_composer.py` / `main.py`

- `continuous_affect` 成为 `ReplyComposerRequest` 和 public/private mint 的 required exact 字段，无 `None` 默认；
- Composer 在生成 Prompt 前调用 canonical inspector；
- Persona Prompt 增加 `continuous_affect` 脱敏投影；
- 当前轮 `AffectAppraisal` 仍决定本轮 trigger，continuous state 只控制连续情绪背景，不能改变 ContentIntent；
- main 只从当前 event 上 P4-01 exact mutation 签发 context，签发失败整轮 fail closed。

## 红灯与迁移证据

首轮红灯为模块导入失败：不存在 `AffectRenderContext` / inspector。

required 字段落地后，旧测试夹具出现 117 个缺参错误；这些全部机械迁移到 module-private test canonical issuer，没有给 production builder 增加默认或兼容旁路。

最终验证：

- P4 affect hot path：`6/6`；
- AffectState + P4 hot path：`18/18`；
- Composer：`15/15`；
- Affect/Composer/ActionOutcome/Validator/P8/Semantic related：`182/182`；
- Windows full：`1067/1067`，skipped 7；
- AstrBot Python 3.12 隔离 container full：`1067/1067`；
- `py_compile` / `compileall` / `git diff --check`：通过。

攻击回归覆盖 context copy、字段 mutation、跨 binding、同字段 binding copy、binding mutation、旧轮 context 在新 state 落盘后失效，以及恢复完整字段后的当前 exact context 可继续检查。

## 生产边界

- 未覆盖线上插件，未重启生产容器；
- live `main.py` SHA256 仍为 `583BF681D28BBAA22CC705F21136BA52CC06A326D7DE7B59A8F559D3D465DB5C`；
- AstrBot running、restart=0、WebUI 200；
- owner adapters 仍因 production allowlist/live output authority 未通过而全关闭；
- 未执行 Git 写操作。

## 唯一下一入口

**P4-04 亚托莉角色校准**：建立当前轮情境 × continuous affect × relationship 的闭集校准矩阵，保证傲娇/逞强只在具体触发出现，随后必须回到在意、行动或当前问题；普通问答、安慰、纠错和不同关系距离不得退化为固定口癖或泛用模板。
