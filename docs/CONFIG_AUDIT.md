# 星汐 0.5.25 typed-only 配置审计

审核日期：2026-08-24

DEF-002 补充审核：2026-08-26；自然参与语义准入没有新增、删除或迁移配置字段。

审核范围：`_conf_schema.json`、`main.py` 与 `core/*.py` 的生产读取点。历史 changelog 和阶段报告只记录当时状态，不是当前配置契约。

## 结论

- 当前配置页共有 **7 个功能分组、57 个叶子字段**；高风险能力没有含糊默认值。
- `natural_group_participation_enabled=false` 且独立白名单为空，所以默认不会在未点名时自然接话；`natural_group_participation_rules` 只编辑决定回复后的表达规则，不绕过开关、白名单、可信上下文、cadence 预检或一次语义裁决。
- 自然参与语义准入固定使用 AstrBot 当前会话 Provider；不读取 `replyer_provider_id`，也没有第二 Provider、关键词、阈值、概率、工具、媒体或 fallback 配置。普通成员与 Owner 使用完全相同的候选和语义资格门。
- `proactive_initiation_enabled=false` 且 `proactive_group_allowlist=[]`，所以默认永不主动开题；`proactive_initiation_rules` 只编辑触发后的续题规则，不绕过调度策略。
- `owner_action_enabled=false`，四个逐适配器开关也全部 false；Shell 永久代码级硬关闭。
- `owner_ids=[]` 只表示没有可信主人资格，不影响普通聊天；即使填写也不授予执行权。
- 星汐不再提供 Meme 总开关、概率、准入回合或类别字段；这些设置全部归 Meme Manager，避免同一功能出现两套互相打架的配置。
- 插件启用后只有 typed 链。旧 architecture rollout、Planner、StyleRetriever、recovery queue 等字段不读取，也不能恢复旧执行器。

## 字段闭集

| 分组 | 字段 |
|---|---|
| 基础回复 | `enabled`、`persona_name`、`replyer_provider_id`、`enable_chat_bubbles`、`chat_max_bubbles`、`bubble_interval_min_ms`、`bubble_interval_max_ms` |
| 称名唤醒 | `natural_name_wake_enabled`、`natural_name_wake_mode`、`natural_name_wake_aliases`、`natural_name_wake_group_whitelist` |
| 群聊自然参与 | `natural_group_participation_enabled`、`natural_group_participation_rules`、`natural_group_participation_allowlist`、`natural_group_participation_min_context_messages`、`natural_group_participation_cooldown_seconds`、`natural_group_participation_window_minutes`、`natural_group_participation_max_joins_per_window`、`social_feedback_enabled`、`social_feedback_window_minutes` |
| 冷场主动续题 | `proactive_initiation_enabled`、`proactive_initiation_rules`、`proactive_group_allowlist`、`proactive_active_hour_start`、`proactive_active_hour_end`、`proactive_timezone_offset_minutes`、`proactive_observation_minutes`、`proactive_idle_minutes`、`proactive_cooldown_minutes`、`proactive_daily_limit`、`proactive_scheduler_interval_seconds`、`proactive_min_bubbles` |
| 上下文与记忆 | `prefer_livingmemory_group_history`、`max_context_messages`、`max_context_chars`、`inject_verified_context` |
| 权限与主人动作 | `owner_ids`、`trusted_bot_identities`、`permission_guard_enabled`、`guest_allowed_tools`、`permission_audit_log`、`owner_action_enabled`、`owner_action_artifact_read_exact_enabled`、`owner_action_artifact_grep_enabled`、`owner_action_memory_write_literal_enabled`、`owner_action_sandbox_shell_once_enabled`、`owner_action_artifact_root`、`owner_action_artifact_path_flavor`、`owner_action_shell_family` |
| 性能与诊断 | `inference_max_parallel`、`inference_max_waiters`、`inference_queue_timeout_seconds`、`inference_active_timeout_seconds`、`performance_window_samples`、`continuity_max_scopes`、`continuity_max_subjects`、`debug_log` |

字段总数由 release-surface test 固定为 7 个分组、57 个叶子字段；新增或删除字段时必须同时改 schema、生产读取、迁移器、默认值、README、测试和本审计。`text` 是 AstrBot 4.27.2 运行时支持的多行字符串类型。P10-19 删除了星汐侧三个 Meme 重复字段；旧配置迁移只丢弃这三个 Manager-owned 字段。0.5.25 新增的 `proactive_min_bubbles` 默认 2，只控制主动首稿的最少语义气泡数，不复制 Manager 设置。

## 默认安全门

| 能力 | 默认 | 额外门 |
|---|---:|---|
| 普通 typed reply | 开 | 可信 ingress、target、Persona、Guard、SendReceipt |
| 普通用户检索 | 精确 `astr_kb_search` 与 AnySearch 两项 | AstrBot 原生知识库配置、只读能力分类、当前 KnowledgeGap、sealed evidence |
| 未点名参与／React | 关 | 独立总开关 + 独立群白名单 + 至少两条可信多人上下文 + continuity／capacity／cooldown／窗口／backoff 预检 + 当前会话 Provider 一次严格语义裁决；明确对其他人零调用。只有语义 `REPLY` 才提交 join；进入既有下游后仅 `MAY_JOIN` 可按知识缺口密封读取一次，`REACT_ONLY` 零检索 |
| 文字后的 Meme 补图 | 星汐无开关或概率 | 普通／自然回复由官方 Meme Manager 4.15.1 正常钩子读取最终回复；主动／冷场通过其公开类别兼容接口。Manager 独占开关、概率、资源包和实际发送 |
| 主动发起 | 关 | 非空群白名单、时段、观察、空闲、冷却、日限额、可信公开话题、每群 topic/final digest 防重复、按需一次密封读取、最少气泡、generation、逐段 send；检索失败则沉默 |
| 主人 artifact read/grep | 关 | master + per-adapter + safe root + live runtime conformance |
| 主人 memory write | 关 | exact private owner scope + live admin conformance |
| 主人 Shell | 关 | 无启用路径，代码硬关闭 |

## 已废弃配置族

- `architecture_v2_mode`、`architecture_v2_rollout_scope`、`typed_context_v2_mode`；
- 独立 Planner／fallback／旧 material budget；
- StyleRetriever／Embedding／Reranker；
- 旧概率型主动接话、旧主动话题 Provider fallback；
- Provider 故障补答与 `pending_replies.json` recovery queue；
- 旧通用口语化／硬截断／多级重试开关。
- `meme_complement_enabled`、`meme_complement_cadence_turns`、`meme_complement_cooldown_turns`；三项已由 Meme Manager 的唯一设置面替代。

当前未点名参与和主动发起是 P5/P7 重新建立的 typed 实现，不读取上述旧字段。

`ambient_participation_rules` 与 `quiet_topic_rules` 只作为升级迁移别名，分别写入 `natural_group_participation_rules` 与 `proactive_initiation_rules`；运行时只读取两个新字段，不形成第二配置源。

## 升级规则

1. 部署前备份插件目录和配置。
2. 不要把旧版字段复制成新的“兼容开关”；未知字段应在验证完成后删除。
3. 应急回退只能恢复完整备份版本，不能在同一进程混用两套运行链。
4. 主人动作、群聊自然参与和冷场主动续题不得因升级自动开启。
5. `X-01` 与 O1 是外部插件生命周期计划，不是配置项；O1 尚未执行。
