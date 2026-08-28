# P10-11 Meme 多轮语境准入热修

状态：完成并部署（0.5.4）。

## 1. 生产症状与归因

- 生产 0.5.3 在 21:29～21:31 收到四个真实人类轮次；四轮均出现 `typed_reply.prepared` 并完成文字发送。
- 同一窗口没有 Shio Meme execution receipt、图片 decode 或平台图片出站证据。
- Meme Manager 每轮的“没有可发送的有效候选”来自其通用语义清理路径，不证明 Shio 调用了受控 compatibility executor。
- 根因位于 Shio admission：旧 `_TEXT_MEME_BLOCK_RE` 把任何问号及“怎么/如何”等全部硬拦，旧轻松词表又不识别“双关/谐音/玩笑/接梗”，四轮因此都在 executor 之前退出。

## 2. 红灯

新增真实四轮脱敏回放后，修复前定向测试真实失败：首轮实际返回旧 `serious_or_request_context`，且后续明确双关与接梗无法达到新合同。目标闭集为：

1. `七夕到了，群友都发烧了怎么办？` → 拦截 `hard_safety_context`；
2. `你要理解一下一语双关。` → 准入 `explicit_playful_context`；
3. `所以七夕到了，群友都“发烧”了怎么办？` → 准入 `recent_playful_continuation`；
4. `对的，快来骂群友变态` → 拦截 `hard_safety_context`。

另加跨发送者与真实风险负例：另一名群友的“双关”不能给当前发送者授权；引号外“我真的发烧了”始终硬拦。

## 3. 修复

- 把永远硬拦的伤害、攻击、辱骂与 literal 医疗/情绪风险从普通问句/任务请求中拆开。
- 只移除成对引号中的被讨论词，再检查 literal 风险；未配对引号不会获得豁免。
- 当前消息明确包含双关、谐音、玩笑、接梗、调侃等 code-owned cue 时可准入。
- 只读取 `AssembledContext.replyer_thread` 中同一发送者最近四条 verified user records；最近三条里存在明确玩笑 cue 且当前轮有“所以/没错/就是/引用”等续接信号时，允许一次 complement。跨发送者记录不参与。
- 模型仍没有 Meme 工具；executor 仍要求 exact plan/expression/generation/runtime/presentation/send evidence，且最多发送一次。
- 每轮新增 `meme.complement_decision`，仅含 trace、eligible、闭集 reason；执行后新增 `meme.presentation_terminal`，仅含 canonical receipt 的 kind/status/attempt/success/reason。正文、用户 ID、query、候选、路径均不记录。

## 4. 验证

- admission 红灯转绿：`1/1`。
- P6 Meme contract + presentation transaction：`18/18`；全部 P6：`22/22`。
- 主 pipeline：`86/86`。
- release surface + deterministic candidate：`11/11`。
- Windows full：`1260/1260`，skipped 7。
- 隔离 FNOS AstrBot Python 3.12 candidate full：`1260/1260`，47.489 秒。
- 生产 95-file live-copy + 完整 harness full：`1260/1260`，47.495 秒。
- 生产 live-copy 关键三链：四轮回放、跨发送者/真实风险边界、文字 terminal 后 compatibility executor 单次图片发送，`3/3`。
- `compileall`、candidate verifier、`git diff --check` 均通过。

## 5. 候选与部署

- ZIP：`astrbot_plugin_shio_v0.5.4_upload.zip`
  - 95 files
  - SHA256 `26E385F32BB73772C68FF9B9886CE131150E0F2F45C519DD5D91B39164D9569F`
- manifest SHA256：`BDFD3485FABE63DE3BB4EEBBBEB1ABEDE1726A313495784B1B047CB2DD133E2C`
- full-source test TAR SHA256：`E233426B4A7866018B345DC6AE542DD20BDB406EA8D070BDD86AC531F78CAE16`
- 备份根：`/AstrBot/data/backups/shio/P10-11-meme-context-hotfix-20260819T135535Z`
- 备份包含部署前 0.5.3 `plugin-copy/`、原 live 目录 `live-moved-original/` 与配置副本。
- 配置 SHA256 部署前后均为 `09528ABB3BDB988EA50DC515E5085EC3AE642DAB7C702E8F6807EF557C62D769`。
- 新 live `main.py` SHA256 `C6140D484D251867836ABCF41F45FDAF7A7B9984B395CB00C382296615C4D9CA`；`metadata.yaml` SHA256 `149753FBE6DA0A4F056C2D49062A4B238FD485CF413886168A61489F1997C5A4`。

## 6. 重启后生产门

- StartedAt：`2026-08-19T13:57:33.686715859Z`。
- 容器 running，非 restarting，OOMKilled=false，RestartCount=0。
- WebUI HTTP 200。
- 0.5.4 load marker：1；`proactive.scheduler_started`：1。
- 新窗口 traceback/error/exception/load failure：0。
- live manifest：95/95 source exact；额外 86 个文件全部是 `__pycache__/*.pyc`。
- 未修改 AstrBot 核心、全局 Provider 回退、LivingMemory、Meme Manager、资源包、Docker Compose 或其他插件。

## 7. 运行边界与中断恢复

重启后尚无新的自然人类消息，所以生产日志中的 `meme.complement_decision=0`、`meme.presentation_terminal=0` 只能说明没有新样本，不能冒充 QQ 平台已经再次发送图片。本次不伪造群消息。执行路径已有 P10-09 的真实图片出站证据，本次变更的 admission 与 terminal 链则由生产 live-copy 关键三链和全量验证覆盖。

如后续自然对话仍无表情，不再要求用户连续盲测：从 StartedAt 新窗口直接查每轮 `meme.complement_decision`；若 eligible=false，reason 已精确归因；若 eligible=true，再查 `meme.presentation_terminal` 的 runtime/status/reason。若需回滚，先停止容器，把当前 0.5.4 live 移到新的保留目录，再将本报告备份根下的 `live-moved-original/` 原子移回；配置未变。
