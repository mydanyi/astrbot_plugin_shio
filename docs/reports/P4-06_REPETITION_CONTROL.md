# P4-06 去模板与复读报告

## 结论

P4-06 已完成本地与隔离 AstrBot Linux container 门，候选未部署。P4 至此工程完成。

旧 `core/dialogue_quality.py` 虽有少量相似度测试，却没有接入 typed 热路径，还在 core 中硬编码 ATRI 名称和三个角色短语。最终 Output Validator 因此会放行连续固定开头、近期角色短语和历史原句复读。

现在 repetition control 只读取 canonical `ReplyComposerRequest.assembled_context.replyer_thread` 中经 verified send receipt 归一化的近期 assistant 回复，并使用 Persona 资产短语和角色无关的动态结构检测；未认证 legacy assistant 文本没有阻断权。

## 红灯

正式红灯覆盖：

- 连续两条历史与当前回复使用同一个“短句 + 第二分句开头”框架，旧 detector 返回空；
- detector 不接受 package phrase 参数，且 core 源码含 ATRI/角色硬编码；
- canonical Output Validator 对固定开头返回 PASS；
- 一次 repair payload 没有 `must_change_visible_form`；
- 只出现一次但占近期短回复显著比例的动态角色短语无法发现。

初轮表现为 failures/TypeError；实现后全部转绿。

## 实现

### 角色无关 repetition profile

`core/dialogue_quality.py` 现在检查：

- 完整相等、较长包含关系和高相似度复读；
- 当前回复与最近至少两条回复相同的有界开头结构；
- 不依赖词表、占较短回复显著比例的动态中文长片段；
- 由当前 Persona package 提供的 catchphrase；
- 当前用户明确要求“原样说/复述”时放行；
- 当前问题中出现的必要术语不作为动态模板片段拦截。

core 源码不再包含 ATRI、苏澄或中性 Persona 的名称和特定台词。

### canonical Validator 与 repair

`core/output_validator_v2.py` 在验证 exact Composer request 后，从其 exact `AssembledContext` 派生最近六条 verified assistant reply，并从 exact Persona package 派生 catchphrase。命中后签发 repairable `dialogue_repetition`；检测输入不接受 caller-supplied history、bool 或 raw Mapping。

`core/repair_controller.py` 继续只有一次 repair generation，并把 issue codes、`must_change_visible_form` 和 `do_not_copy_rejected_reply` 作为闭集约束传入；不暴露被拒绝正文，不新增常规二次风格改写。

Renderer Prompt 同时提前要求避免完整复制、固定开头和已使用情境短语；必要术语与当前问题关键词可正常重复。

## 验收边界

- 连续“才没有，我只是……”结构：repair；
- “高性能机器人”等未硬编码但跨轮显著复用的角色片段：repair；
- Persona package 中完整情境短语复用：repair；
- 完整/高相似历史复述：repair；
- 用户明确要求复述：允许；
- Docker/Kubernetes 等当前问题必要术语：允许；
- 未认证 legacy assistant history：不获得阻断权；
- repair 后仍重复：现有 REPAIR phase 验证再次拒绝，不进行第二次生成。

## 验证

- repetition targeted：`10/10`；
- Core/Composer/Validator/Repair/Semantic/Pipeline related：`206/206`；
- Windows full：`1081/1081`，skipped 7；
- AstrBot Python 3.12 隔离 container full：`1081/1081`；
- `compileall`、merge-marker、trailing-whitespace：通过。

## 生产边界

- 未覆盖线上插件、未重启生产容器；
- live `main.py` SHA256 仍为 `583BF681D28BBAA22CC705F21136BA52CC06A326D7DE7B59A8F559D3D465DB5C`；
- AstrBot running、restart=0、WebUI 200；
- production adapter allowlist/live output authority 仍未开放，四 adapter 零执行；
- 未执行 Git 写操作。

## 唯一下一入口

**P5-01 Attention Gate**：在保持 direct current-turn 路径不变的前提下，只让 `ACCEPT_HUMAN` 事件进入 opportunity attention；ban/self/known-bot/plugin-echo 全部零副作用。先建立 `DIRECT / ABOUT_SELF / OPEN_QUESTION / OTHER / UNCERTAIN` 与 scene/relationship/rate state 的 typed 输入合同，再决定是否允许进入 P5-02 Participation Engine。
