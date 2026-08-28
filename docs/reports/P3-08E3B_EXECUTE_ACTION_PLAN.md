# P3-08E3B：独立 EXECUTE_ACTION Typed Plan

日期：2026-08-18（Asia/Hong_Kong）

## 交付结论

E3B 已在不接 `main.py` 的前提下建立独立 owner action plan：

- `ActionKind.EXECUTE_ACTION` 与 exact `OwnerActionOperation`；
- operation 进入 deterministic action ID；
- Planner 只从 E3A `OwnerActionRouter` 的 opaque canonical route 生成动作；
- 公开 proposal、模型 suggestion、guest/untrusted/group policy、copy/cross-router/cross-ticket route 均不能生成 `EXECUTE_ACTION`；
- `USE_TOOL` 收窄为 KnowledgeGap capability-only 取证；
- Controller 只 seal `EXECUTE_ACTION`，并精确核对 route proposal operation；
- adapter 编译失败前不 claim route，可显式形成无 adapter/参数的 canonical denial；
- public controller shape 的 bare-string Enum fail closed。

## 先红后绿

红灯覆盖：缺少 ActionKind、operation 未进入 digest、旧 owner broad→USE_TOOL、proposal 无 provenance、route copy/cross authority、Controller 仍认 USE_TOOL、seal 过早 claim、adapter disabled 后无 denial、bare-string Enum 与伪 target carrier。

绿灯结果：

- Controller 24/24；
- E3B focused 76/76；
- related 184/184；
- full 841/841。

## 安全边界

- 不执行真实工具；
- 不修改 Router、D adapters/runtime collector、output、composer、guard、main 或配置；
- 不修改 FNOS；
- 不执行 Git/GitHub 写操作；
- Shell 保持 descriptor + code-level hard-disable；
- LivingMemory 生产 scope/permission 不合格时继续 fail closed。

Controller 的二轮测试夹具使用 canonical memory runtime evidence，并只在 `try/finally` 内临时切换 memory descriptor 的 confirmation policy；退出编译立即恢复，既不执行工具也不改变生产默认。

## E3C0 后续状态

E3C0 已闭合原第 1、2 项：Router 提供 route+ticket composite/no-op；Planner 增加 bounded exact `PlannedActionAuthority`；confirmation 生成 current plan，并由 Controller 签 current+origin continuation lineage。CONFIRM 是 EXECUTE_ACTION，DENY/CANCEL 是 REPLY。

仍为硬红灯：

1. request/denial/receipt 的 `ActionOutcome`、ContentIntent、终止/错误回复、renderer 与 final guard 尚未接线；
2. `complete_execution()` 仍接受 raw status/effect/output，必须迁移到 E2C sealed completion；
3. terminal ledger 需要 TTL/tombstone 清理；pending/in-progress 不得驱逐，evicted canonical object 必须永久 fail closed；
4. `main.py` 唯一热路径和 production adapter conformance 尚未闭合。

只有这些硬红灯闭合后，E3B 才能进入 main 唯一生产热路径。
