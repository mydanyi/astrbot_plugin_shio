# P5-04 Participation React-only 报告

## 1. 结论

P5-04 已完成 Windows 与隔离 AstrBot Linux container 门，候选未部署。

生产热路径现在可以把一个 exact canonical `ParticipationCadenceDecision` 收敛成 code-owned `ParticipationReactionDecision`。只有已通过 P5-02 价值判断、P5-03 cadence 预算、地址为 `ABOUT_SELF`、内容短且明确轻量的社交候选，才会得到 `REACT_ONLY`。Planner 随后只能形成 targeted `REACT`，并建立本轮 `ExpressionIntent(modality=REACTION, social_act=light_reaction)`。

本层不查询、不调用、不发送 Meme；实际 Meme Manager 唯一执行契约仍属于 P6。

## 2. 先红后绿

正式红灯为新增 `tests/test_p5_participation_reaction.py` 后导入失败：

- `ModuleNotFoundError: No module named 'astrbot_plugin_shio.core.participation_reaction'`；
- 证明原热路径虽然已有 `REACT_ONLY → REACT` 的 Planner 合同，却没有任何 code-owned P5 决策可以签出该状态。

根修后定向 `6/6`：

1. 轻量 `ABOUT_SELF` 可只形成 React；
2. `REACT` 为零文字 Prompt、零模型、零工具；
3. DIRECT、严肃场景、OPEN_GROUP 与 NO_ACTION 不变成 React；
4. owner/private 高权限 Action 保持 `EXECUTE_ACTION`，不会降级成 React；
5. copy、cross-runtime、字段篡改与 replay fail closed；
6. ledger 有界，trace/repr 不含正文、sender、scope 或目标标识。

## 3. 实现边界

### 3.1 Canonical reaction authority

新增 `core/participation_reaction.py`：

- 每个 exact `ParticipationCadenceAuthority` 只能建立一个 reaction authority；
- `issue(cadence, current_message=...)` 只接受 exact cadence 与正文摘要完全匹配的当前消息；
- 每个 cadence source 只能签一次；
- 决策、嵌套 `ParticipationDecision`、binding、authority、seal 与完整字段快照均在 inspect 时重验；
- ledger 使用 weak exact records，默认上限 1024；
- 模块无 Provider、工具、Meme、owner-action 或发送入口。

### 3.2 轻量闭集

React 只在以下条件同时满足时签发：

- cadence 最终仍是 `MAY_JOIN`，且本轮确实占用了 join 预算；
- exact Address 为 `ABOUT_SELF`；
- 当前消息不超过 80 字，含高置信轻量社交标记；
- 不含问句、求助/执行请求、故障/修复、危险、隐私、照护、争执或其他严肃标记。

条件不满足时，P5-04 不改写 cadence 原决策。它不会把 DIRECT、WAIT、NO_ACTION、OPEN_GROUP、知识取证或主人动作升级/降级成 React。

### 3.3 热路径

`main.py` 现在：

1. 在 P5-03 cadence 后立即签发并保存 exact reaction decision；
2. `MAY_JOIN` 与 `REACT_ONLY` 都可触发 AstrBot 进入 typed build，但原因码分开；
3. build 阶段重新 inspect assessment、cadence、reaction 的 exact 同轮链；
4. `REACT_ONLY` 固定 KnowledgeGap=`NONE`，不会产生 acquisition；
5. Planner 形成 `REACT` 后构造本轮 `ExpressionIntent(REACTION)`，随后清空 Prompt、上下文、媒体和工具并终止文字生成。

## 4. 验证证据

- formal red：模块缺失 `ModuleNotFoundError`；
- P5-04 targeted：`6/6`；
- P5/Planner/Address related：`51/51`；
- Windows full：`1101/1101`，skipped 7；
- 隔离 AstrBot Python 3.12 container full：`1101/1101`；
- `compileall`、限定 `git diff --check`、静态 forbidden-import/source 检查通过；
- 第一次容器暂存因包目录名错误得到 `0 tests`，该结果未计入验收；修正为 `/tmp/.../astrbot_plugin_shio` 后重新完整运行并取得 `1101/1101`；
- 所有 FNOS/container `/tmp` 暂存已按精确路径清理；生产容器 `running=true`、`restart=0`、WebUI 200；
- 生产 `main.py` SHA-256 仍为 `583bf681d28bbaa22cc705f21136ba52cc06a326d7de7b59a8f559d3d465db5c`，未覆盖、未重载、未重启。

候选冻结哈希：

- `core/participation_reaction.py`: `b0cd1f3eadbd2adefddaa1e252744c033055a8058d94ab5bc2693f31760a2b68`；
- `tests/test_p5_participation_reaction.py`: `609cb0e595f14a93453a6b1547c1332ad64aa29dbcf8ca021e90dff52e99efad`；
- `main.py`: `c06562c6e8927514e33630a7a785667988f1c7e57783d7499854754717bf8fc6`。

## 5. 改动范围

- 新增 `core/participation_reaction.py`；
- 新增 `tests/test_p5_participation_reaction.py`；
- 修改 `main.py` 接入 exact reaction decision 与 typed Reaction `ExpressionIntent`；
- 新增本报告并更新 `SHIO_MASTER_PLAN.md`。

未修改第三方插件、AstrBot 核心、配置 schema、FNOS 生产插件或 Git 状态。

## 6. 下一唯一入口

下一唯一入口是 **P5-05 新消息打断与重规划**：把现有 generation epoch/cancellation 从直接文字路径扩展到自然参与的 `MAY_JOIN` 与 `REACT_ONLY`。同 scope 新 accepted human revision 到达后，旧自然参与的规划、生成、React/未来 Meme presentation 都必须失效；跨 scope 不互相取消。P5-05 仍不执行 Meme；P6 才建立唯一表情执行链。中断后从本段和 `SHIO_MASTER_PLAN.md` 第 12 节恢复，不重做 P5-04。只有 P10 才请用户统一测试效果。
