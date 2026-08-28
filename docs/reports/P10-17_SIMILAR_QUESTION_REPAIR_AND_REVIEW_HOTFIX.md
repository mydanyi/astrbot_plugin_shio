# P10-17 相似问题、repair 空回复与审查缺陷修复

## 结论

0.5.17 已完成实现、全量重新审查、Windows／FNOS 隔离候选／FNOS 线上实际副本验证、带自动回滚的生产部署和真实群聊验收。生产配置未改；用户已确认相似问题没有串答，普通问题能关联前文。本次缺陷端到端闭环。

## 真实缺陷时间线与证据边界

- 第一条问题曾正常回复；下一条只改变了数字／否定关系的相似问题，主模型产生输出后触发重复守卫。
- repair 路径随后异常，旧实现把异常候选折叠为空字符串；最终只记录 `empty_visible_reply`，平台没有可见回复。
- 后续一次重问又出现旧答案的语义改写，证明问题不只是 provider 偶发失败，还包含当前问题 anchor、问答配对和关系漂移审查缺口。
- 旧日志没有保留 repair provider 的具体异常类型和各返回通道长度，故不能事后声称已知道上游异常正文。本轮只修复已由证据证明的吞异常、错误豁免、错误放行和可观测性缺口。

## 根修

- `current_question_anchor.py` 对算式、数字、等于／不等于关系和标点分隔对象建立 typed relation assertion。
- `dialogue_quality.py` 将近期 assistant 答案与其对应的 prior user question 成对比较；只有当前问题与旧问题实质等价时，相同答案才可豁免。
- `output_validator_v2.py` 新增 current relation drift 检查；旧关系对象被换皮复述时拒绝，显式比较旧值和新值时不误拦。
- `main.py` 与 `repair_controller.py` 区分 provider exception、reasoning-only、empty completion、validation rejection 和 visible completion。直接问答在 repair 不可用时给出仍经过 REPAIR validator、semantic guard 和 final-send seal 的诚实可见 fallback，不再静默。
- `observability.py` 允许无敏感内容的数值型 `*_token_count`，同时继续丢弃 token／credential／secret 等凭据字段。

## 审查缺陷修复

- 新增根目录 `AGENTS.md`、`HANDOFF.md`、`docs/REVIEW_PLAYBOOK.md` 和 `docs/PITFALL_LEDGER.md`；以后每次诊断、修改、打包和部署前必须先读。
- 测试同时覆盖 false block、false pass、完全相同问题、只改数字关系、自然正确回答、provider 异常、纯 reasoning、空 completion 和校验拒绝。
- 重新运行此前已通过的完整测试，而不是只跑新增测试。首次全量复审暴露 `23 failures + 6 errors`：均逐项核对，保留自然入场的显式开关／白名单／上下文门，修正绕过新契约的旧测试夹具和旧人格／主动策略断言。
- 最终 Windows 全量 `1313/1313`，skipped 7；compileall 与 diff-check 通过。
- FNOS 隔离候选第一次因 harness 漏带 README、CHANGELOG、AGENTS、HANDOFF 出现 5 个缺文件错误；补齐显式清单后从头复跑 `1313/1313`。该坑已记为 P-028。

## 发布与生产证据

- deterministic ZIP：`astrbot_plugin_shio_v0.5.17_upload.zip`。
- 96 个发布文件，535284 bytes；连续两次 SHA-256 都是 `52F765520347E5345D1AEE1CC96D25655BFA823687BDD6FC43F98E2CA6F44C6E`。
- 三组 P10 runner：24/24、6/6、29/29，均无 failure/error；隔离 candidate full `1313/1313`。
- 写前生产为 0.5.16；备份与事务根：`/AstrBot/data/backups/shio/P10-17-semantic-repair-20260823T194300Z`。
- 事务阶段：`PREPARED -> CONTAINER_STOPPED -> OLD_PLUGIN_SAVED -> NEW_PLUGIN_LIVE -> CONTAINER_STARTED -> VERIFIED`；异常会恢复 `live-moved-original` 并重启。
- 线上实际目录 96/96 与 manifest 精确一致；从实际目录重建的 live-copy full `1313/1313`，三组 P10 runner 再次全绿。
- 配置 SHA-256 部署前后均为 `06C08086BAF4D486AC6AF77BE3809DD1C30923DD3928125A35F017A1733D191D`，没有迁移或改写。
- 容器 running=true、restarting=false、OOMKilled=false、RestartCount=0，WebUI HTTP 200；新启动窗口 `ERROR/CRITICAL/Traceback=0`，0.5.17 加载标记 1 次。

## 真实群聊验收

- 同群依次发送两条形式相似但关系断言相反的问题，再发送一条要求关联前文的普通问题；不记录消息正文或真实身份。
- 三轮 `group_history.read=verified`，每轮读取 16 条；Composer 实际上下文分别为 14、17、20 条。
- 三轮均为 primary-only、guard hit 0、repair 0、model failure 0；发送终态分别为 2、2、1 段成功，没有 empty reply、ERROR 或 Traceback。
- optional LivingMemory recent reader 同时报告 interface degraded，但这是独立的 memory recall 路径，不能覆盖 verified group history 的正证据。
- 无正文日志只证明链路与发送；用户随后明确确认三条可见回复均正常，第二条未复用第一条答案，第三条正确关联前文。至此语义层和发送层均通过。

本次还固化三条审查结论：方法被证伪时旧结论必须失效并全量重审；不得为修旧测试拆除生产安全门；content-free trace 不得冒充可见语义验收。对应 P-029～P-031 与 release-surface 自动测试不得删除。
