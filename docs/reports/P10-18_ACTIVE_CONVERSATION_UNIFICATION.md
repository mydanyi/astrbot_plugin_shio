# P10-18 主动会话链路统一

## 目标

修复“群聊自然参与”和“冷场主动续题”在生成、检索与呈现能力上的断裂，同时保留两者彼此独立的触发开关、频率与调度策略。

本阶段先产出可审查候选；用户第二次明确批准后，已完成生产部署、配置迁移和容器重启。专用群真实消息语义验收仍是独立的最后一门。

## 已冻结基线

- 群聊自然参与会进入 typed 普通回复链，拥有完整 Persona、已验证公开群聊上下文、校验／修复、气泡拆分与普通 Meme 补充；但 `group_join` 在能力策略和 broker 中被强制拒绝外部只读检索。
- 群聊自然参与即使判断到知识缺口，也会被固定改写成 `opportunity_join_zero_tool`。
- 冷场主动续题拥有独立触发和状态机，这一边界正确；但其生成与呈现是另一条旁路：只投影前四项性格、禁止工具、固定一个气泡、直接 `Context.send_message`，没有复用普通呈现与 Meme 能力。
- 当前 WebUI schema 缺少“自然接话完整规则”和“冷场主动续题完整规则”；旧版字段已经从运行配置表面消失。
- 旧测试把“零工具 + 单段发送”写成正确结果；旧报告把 Meme、主动续题各自测试通过当成二者已经集成。这两类结论在 P10-18 范围内作废。

## 修复合同

1. 恢复两个 WebUI 可编辑文本框：
   - `natural_group_participation_rules`
   - `proactive_initiation_rules`
2. 两类触发继续各自拥有独立总开关、白名单、冷却与调度状态，不合并触发逻辑。
3. 触发之后共享普通会话的完整 Persona 投影、服务器时间、已验证公开上下文、语义气泡拆分、发送回执和 Meme Manager 适配器。
4. 自然参与的 `MAY_JOIN` 可按知识缺口使用一次只读检索；`REACT_ONLY` 永远不检索。
5. 稳定知识和网络黑话优先 AstrBot 知识库；明确实时、需核实的信息才使用联网搜索。
6. 最终 responder 不持有工具；检索只能经 sealed acquisition，禁止写入、执行、设备控制与个人记忆。
7. 冷场续题不能为“找话题”滥用检索；没有可靠公开话题、检索失败或证据不足时保持沉默。
8. 冷场续题没有虚构的“最后发言者”，不引用私聊或个人记忆，不把公开群上下文冒充为某个人的信息。
9. 多气泡逐段发送，每段都有确切回执；过时、失败或部分发送不能记作完整成功。
10. Meme 仅在文本全部成功后作为补充，并继续由 Meme Manager 决定最终出图概率；Shio 只做情境类别与安全节奏判断。

## 审查假设与反证测试

| 编号 | 待证伪假设 | 必须失败的基线测试 | 修复后的通过条件 |
|---|---|---|---|
| H1 | 配置表面已经覆盖完整规则 | schema 查找两个规则字段 | 字段、默认值、分组与旧键迁移均存在 |
| H2 | `group_join` 已能调用知识库／联网搜索 | 自然参与提出稳定知识或实时问题 | 只选择匹配的只读工具，最终模型无工具 |
| H3 | 冷场续题与普通会话共享完整人格 | 检查 prompt 中完整 Persona 材料和自定义规则 | 不再只截取前四性格，规则逐字进入可信 system 区 |
| H4 | 冷场续题支持普通气泡呈现 | 模型返回两条语义气泡 | 两段顺序发送并分别成功记账 |
| H5 | 冷场续题已经接入 Meme Manager | 文本发送成功且情境适合表情包 | 走同一 Meme Manager 适配器；不适合时不调用 |
| H6 | “各模块分别通过”足以证明集成 | 同一端到端用例同时观察上下文、检索、气泡、Meme、回执 | 所有能力绑定同一个 canonical request／presentation |

## 证据日志

- 2026-08-24：完成只读基线核对；H1-H6 均有代码或旧测试反证，尚未修改生产代码。
- 正式红测：规则字段查找报 `KeyError`；冷场 prompt 缺完整规则／Persona；双气泡只发送一段。共 3 个用例出现 2 failure + 1 error，证明旧实现不满足合同。
- 第一轮相关修复后，136 项主动／参与／检索／Meme／配置测试全绿；第一次全量为 1322 项、3 个失败，全部属于发布表面仍写死旧 57 字段与旧类型白名单。用线上 AstrBot 4.27.2 源码核实 `DEFAULT_VALUE_MAP` 和保存校验均正式支持 `text`，随后把发布合同更新为 59 字段。
- 差异复审额外发现并修复：旧 0.5.17 配置缺两项新规则时迁移会失败；群内主人自然参与可能越过管理员工具白名单；普通回复与主动续题各自复制 Meme Manager transport；冷场首稿被拦后没有一次受控 repair。这四项均先补反向用例再重跑。
- 最终 Windows：`1328` 项通过，`skipped=7`，0 failure/error；`compileall` 通过。三组固定矩阵分别 `24/24`、`6/6`、`29/29`。
- H6 联合用例在同一个 proactive request 上同时证明：匿名可信群上下文、AstrBot KB sealed evidence、完整 Persona 与自定义规则、最终 `func_tool=None`、两个语义气泡、两个成功回执、文字后 exact `food` Meme Manager marker。
- 候选：`astrbot_plugin_shio_v0.5.18_upload.zip`，98 个运行文件，543582 bytes，SHA-256 `186ceea979efa5425fc8d1f28eb07678d7aaeb903e0713b985ee70ec962a9fb3`；manifest 独立验签通过。
- 隔离容器使用线上相同 RepoDigest `soulter/astrbot@sha256:6ff58843ffcb285da4031fb6f11a27a581c89c84eadcf2db08b4d795d45b1487`、`--network none`、只读根与只读候选挂载。首轮 1328 项仅因 harness 漏 `LICENSE` 出现 1 个文档链接失败；按 P-028 补齐显式清单后从零重跑 1328 项全绿。
- 真实 AstrBot 源码导入候选通过：8 个配置 object、59 个叶子字段、两项规则类型为 `text`。只读根下 AstrBot 自身需要创建 `/AstrBot/data`，测试只为该路径提供一次性 tmpfs；候选目录继续只读。
- 部署前再次只读复核：容器 `running=true`、`RestartCount=0`，仍加载 `0.5.17`；metadata/main/config SHA-256 与批准前快照完全一致，没有漂移。
- 2026-08-24T02:15:56Z 至 02:16:13Z 使用整体目录替换事务部署 0.5.18。停机前先在数据卷备份并用容器内 Python 验证候选 98 文件；事务最终状态为 `VERIFIED`。
- 配置从 8 组/57 项迁移为 8 组/59 项。专用校验器逐组逐键证明旧 57 项值完全不变，只新增 `natural_group_participation_rules` 与 `proactive_initiation_rules` 两项非空规则；两个总开关和两个白名单均保持原值。
- AstrBot 启动后会重排配置文本，迁移暂存 SHA-256 `9261f02d1fb9b33cd261b7ed3a98877a2369a77441f6c006a29d62138f83d9f3` 因而变为线上 `856896b0dca578840b51743e87439280c00a7cbe450653002c2bd976398bcb86`；对启动后的文件再次与原配置做语义比较仍为“旧值全部保持、仅新增两项规则”。
- 线上 0.5.18 metadata/main/schema SHA-256 分别为 `90bea56871eb687201026ab7330e024d5c63bfda54f9734a4ef1981834d14a84`、`a06029aea2a605f018d87b478b91e4a2ff7c5ee91b488431cf6e52436d645c04`、`f26252dfb13482e9745bdfcd688f0dc88fa18cdff74d9d3ec5f13a431be35265`，逐文件 manifest 再验通过。
- 重启后容器 `running=true`、`restarting=false`、`OOMKilled=false`、`RestartCount=0`，WebUI HTTP 200；插件管理器明确加载 0.5.18；live 插件目录唯一；`proactive.scheduler_started=1`、scheduler failure 0；启动窗口 WARNING/ERROR/CRITICAL/Traceback 均为 0。
- 用户既有参数保持：活跃 08:00-03:00、观察 10 分钟、冷场 10 分钟、冷却 30 分钟、每天 20 次、调度 60 秒；自然参与、冷场主动续题、Meme、自然称名仍启用，称名模式仍为 `contains`。
- 完整回滚证据保存在 `/AstrBot/data/backups/shio/P10-18-active-conversation-20260824T021500Z`，含旧插件两份副本、原始配置、候选 ZIP/manifest、迁移器、部署器和验证器；远端 `/tmp` 暂存已精确清理。

## 当前门状态

- 实施门：通过。
- 候选门：通过。
- 生产门：通过；0.5.18 已部署并完成启动态验证。
- 真实消息门：待专用群验证自然 `MAY_JOIN` KB/联网、冷场 KB/联网、双气泡、repair、Meme 和关闭开关零触发。没有用户可见语义确认前，不能把 P10-18 宣布为端到端完成。

## 真实效果复核失败（2026-08-24，只读）

- 冷场续题线上实际发送 2 次，均只有 1 个 canonical segment；两次主动 Meme 均在 Shio 前置决策处 `eligible=false`。
- 普通／自然入站 Meme 决策同期为 10 次，仅 1 次 eligible；其余被 `serious_or_request_context=7`、`message_not_lightweight=1`、`unsupported_situation=1` 拦截。唯一进入 Meme Manager 的回合成功发送图片。
- Meme Manager 当前 nested 情感概率与混合消息概率均为 100；源码优先读取 nested 新配置。低图片率发生在 Manager 概率之前，不是配置未保存或 100% 骰子失效。
- H4/H5/H6 的预置两行输出与明确 `food` 话题只证明能力可达，不能证明真实弱模型／真实话题分布的气泡率和图片率。该部分效果结论作废，按 P-029、P-035、P-036 重新审查；生产 0.5.18 暂不改动。
