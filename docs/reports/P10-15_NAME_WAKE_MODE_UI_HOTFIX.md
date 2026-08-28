# P10-15 称名唤醒模式 UI 热修

## 状态

- 阶段：完成并部署
- 生产版本：0.5.8
- 用户目标：生产使用“关键词出现即唤醒（contains）”
- 恢复入口：`SHIO_MASTER_PLAN.md` 第 0、7、12 节与本报告

## 根因

`natural_name_wake_mode` 没有从运行代码或配置合同中删除；生产当前值仍为 `natural`，`classify_name_wake()` 也继续实现 `contains`。问题来自 51-field schema 的插入顺序：总开关、别名和群白名单在前，模式字段却落在全部主动发起设置之后。WebUI 按 schema 顺序渲染，用户在称名设置附近看不到模式，等价于配置表面失联。

## 红灯

新增 release-surface 测试要求称名设置连续排列为：

`enabled -> mode -> aliases -> group_whitelist`

首次运行稳定失败，实际第二项为 `natural_name_wake_aliases`，第四项已经进入 `meme_complement_enabled`，证明模式没有出现在称名组内。

## 实现

1. `natural_name_wake_mode` 移到称名总开关之后，不新增字段、不改变 51-field 数量。
2. WebUI 名称改为“称名唤醒方式”，选项明确为“自然语言判断（推荐）”和“关键词出现即唤醒”。
3. `contains` 的运行语义保持：当前正文命中任一别名即升级为直接唤醒；URL、代码和明确作品标题仍不冒充当前直呼。
4. 新增主链测试，证明配置为 `contains` 后，句中普通别名提及在 admission 后升级为 AstrBot 原生直接唤醒。

## 验证

- UI/运行 focused：`22/22`；包含 schema 连续分组、两种 closed option、纯分类和真实 Pipeline admission。
- Windows full：`1277/1277`，skipped 7，54.292 秒；compileall、tracked diff check、目标尾随空白检查通过。
- deterministic 0.5.8 candidate 连续两次构建字节一致：95 文件、509505 bytes、SHA256 `ECEB58CAA087DF820F3C17DC36075FD07C8B304049DCF63CF1866DF946443E19`；manifest SHA256 `B9696AB011862BEEBD709B575F1566103EBEEA645B66ED54F54D9E87E714FE82`。
- FNOS 隔离 candidate：`1277/1277`，47.618 秒。
- 部署后 production live-copy：`1277/1277`，47.717 秒。

## 生产部署

- 写前版本 0.5.7、51 字段、模式 `natural`；容器健康且 WebUI 可达。
- 备份：`/AstrBot/data/backups/shio/P10-15-name-wake-mode-20260819T185734Z`；包含原 live 插件、写前配置、候选 ZIP/manifest/harness、迁移与部署脚本、durable `deploy.state`。
- 部署事务：`PREPARED -> CONTAINER_STOPPED -> OLD_PLUGIN_SAVED -> NEW_PLUGIN_LIVE -> OLD_CONFIG_SAVED -> NEW_CONFIG_LIVE -> CONTAINER_STARTED -> VERIFIED`；异常会恢复 0.5.7 与原配置后重启。
- 配置迁移严格保留 51 项，只改 `natural_name_wake_mode: natural -> contains`；写前 SHA256 `3DBF0E9144590864B46A2CD2654065885BA655E783B9DFF35DB01987780CE0CE`，写后 `7B325F4B9F53578286B29487CF313C1B64363D32FF6D35F2F3E7A50F80363D93`。
- 当前 0.5.8，95/95 非缓存源码与候选一致；StartedAt `2026-08-19T18:58:50.878373867Z`，running=true、restarting=false、OOMKilled=false、RestartCount=0，WebUI HTTP 200。
- 新启动窗口 Traceback 0、ERROR/CRITICAL 0；0.5.8 加载一次，主动 scheduler 正常启动。
- 生产 schema 连续顺序为 `enabled/mode/aliases/group_whitelist`，中文选项为“自然语言判断（推荐）/关键词出现即唤醒”；持久配置为 `contains`。
- 对真实 live 模块执行不含平台写入的决策探针，“大家刚才提到亚托莉的发型”得到 `direct / 配置为名字出现即唤醒`。

## 边界与恢复

`contains` 指当前消息正文出现任一已配置别名即可唤醒；URL、代码与明确作品标题仍被排除，不把技术内容或《ATRI》作品讨论冒充直接叫机器人。群白名单继续生效，私聊仍由 AstrBot 原生唤醒。

若会话中断，从本报告和 `SHIO_MASTER_PLAN.md` 第 0、7、12 节恢复。部署状态为 `VERIFIED`；回滚时停止容器，把 0.5.8 live 移入备份新保留目录，再原子恢复 `live-moved-original/` 与 `live-config-original.json` 并重启。O1 仍未激活。
