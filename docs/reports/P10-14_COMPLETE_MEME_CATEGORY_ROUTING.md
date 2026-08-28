# P10-14 Meme 全类别情境路由

## 状态

- 阶段：完成并部署
- 目标版本：0.5.7
- 恢复入口：`SHIO_MASTER_PLAN.md` 第 0、7、12 节与本报告
- 第三方边界：不修改 Meme Manager、资源包、AstrBot 核心或最终概率设置

## 根因

0.5.6 虽已修复截图中的 `playful_provocation -> annoyed`，但文字补图执行器仍只有 7 个返回类别；React 的 `acknowledge/light/playful` 仍固定映射为 `happy`。因此资源包的 22 个图形目录并没有全部进入星汐的真实决策链，多个不同社交动作仍可能被压成同一开心图。

线上只读 manifest 的唯一类别闭集为：

`agree/angry/annoyed/awkward/confused/cute/encourage/food/happy/love/proud/reject/request/risky_banter/sad/shy/sleep/surprised/tease/thinking/watching/work`

## 失败证据

新增 `tests/test_p10_meme_category_routing.py` 后，首次定向运行在导入阶段失败：

```text
ImportError: cannot import name 'MemeCategory'
Ran 1 test / FAILED (errors=1)
```

这证明旧实现没有 22 类 code-owned 合同，而不是已有合同只差少量关键词。

## 当前实现

1. `MemeCategory` 精确枚举固定 live manifest 的 22 个键。
2. `select_meme_category()` 只读取当前消息、canonical Affect 标签、闭集社交动作、可信关系和同发送者有界近期语境；模型、引用正文和自由 query 不拥有类别权威。
3. 22 类均有高置信脱敏情境；严重安全语境、未知语义、平衡引用内的孤立情绪词返回无图。
4. `risky_banter` 只接受 `PRIMARY_BOND` 与明确同意式玩笑；peer/unverified 均失败关闭。
5. React 的五个闭集动作分别映射为 `agree/cute/tease/love/encourage`，不再统一 `happy`。
6. `ExpressionIntentAuthority` 在 exact current-message digest 与关系复核后重新计算类别并自行加入唯一 `meme_category_*` 标签；调用方不能注入、复制或跨消息替换。
7. 执行器不再从宽泛 Affect 标签猜图，只消费一个 authority-sealed 类别标签；缺失、多值、未知值全部抑制。
8. Meme Manager 仍是唯一检索/发送执行器及唯一最终概率源；Shio 不新增概率字段。

## 完整验证

- 22 类纯分类 + cadence + canonical ExpressionIntent 路径逐项可达。
- unknown/quoted/hard-safety 零图；普通“办法没用”不误判为角色受辱。
- caller category 注入、跨 current-message、多个/未知类别标签均拒绝。
- P10-14 + P6 focused：`28/28`。
- Meme/React/transaction/release/candidate focused：`48/48`。
- 收紧攻击词边界后的 P10-14 + P6 focused：`28/28`；Meme/React/transaction/release/candidate focused：`48/48`。
- Windows full 两次均为 `1275/1275`、`skipped=7`；最后一次 51.855 秒。
- `compileall`、目标 diff check 和尾随空白检查均通过。
- 0.5.7 deterministic candidate 连续两次构建字节一致：95 文件、509481 bytes、SHA256 `00A5CE00011C495283AEF0D08EA4212B9E781E39C0817EB2BFFE29D4D867D359`；manifest SHA256 `B86E5319BC03CD61F7B0AE13D1A69BD07FF0FDACB572B27D51F9BD615F47650B`。
- FNOS 隔离目录直接解包上述 ZIP、叠加只读测试 harness：`1275/1275`，47.317 秒。
- 部署后从真实 live 插件目录重建副本：`1275/1275`，48.270 秒。

## 生产部署

- 写前生产为 0.5.6，容器 running、RestartCount=0、WebUI 可达；Shio 配置 51 项，Meme 补图为 `true/4/4`，owner action 总开关仍为 false；Meme Manager 的 `emotions_probability=50`、`mixed_message_probability=50`。
- 独立备份：`/AstrBot/data/backups/shio/P10-14-complete-meme-routing-20260819T181752Z`。其中保留原 live 插件、写前配置、候选 ZIP/manifest/harness、部署脚本和 durable `deploy.state`。
- 部署采用同数据卷 rename，阶段为 `PREPARED -> CONTAINER_STOPPED -> OLD_PLUGIN_SAVED -> NEW_PLUGIN_LIVE -> CONTAINER_STARTED -> VERIFIED`；阶段内异常会停止容器、恢复 0.5.6 原目录并重启。
- 配置没有迁移或改写；线上与写前副本 SHA256 同为 `910EC9F0373DBD6E481C4B24251FDFFAC8B502F72F3E5366116CAD2D17111609`。
- 当前生产 0.5.7；95/95 非缓存源码与 candidate manifest 精确一致。容器 StartedAt `2026-08-19T18:19:25.157734672Z`，running=true、restarting=false、OOMKilled=false、RestartCount=0，WebUI HTTP 200。
- 新启动窗口：Traceback 0、ERROR/CRITICAL 0、Meme 初始化失败 0；0.5.7 marker 4，主动 scheduler marker 2。

## 运行边界与恢复

本阶段证明的是：22 个 live 类别都能从不同的当前情境，经同一 code-owned 分类、cadence、canonical ExpressionIntent 和唯一执行标签到达；并且未知、引用、危险或关系不满足的情境不会猜图。它不伪造 QQ 消息来冒充 22 类都已在真实群聊逐一出图。Meme Manager 仍按网页设置的 50% 做最终抽样，所以合适情境也可能不发图；一旦发图，类别不再回退为统一 `happy`。

若会话中断，从本报告和 `SHIO_MASTER_PLAN.md` 第 0、7、12 节恢复。生产部署状态为 `VERIFIED`；回滚时必须先停止容器，把当前 0.5.7 live 移到备份下的新保留目录，再把 `live-moved-original/` 原子移回并重启。O1 仍未激活。
