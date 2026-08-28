# P7-03 公共话题与 Persona 兴趣选择报告

## 1. 结论

P7-03 已完成；候选未部署，主动模型调用与发送仍未启用。

星汐现在只能从 exact P7-02 `ADMITTED` decision、该群当前 canonical public scene，以及已加载 Persona Package 的公开 participation interests 签发一张 typed `ProactiveTopicPlan`。计划明确保持 `model_authorized=False`、`send_authorized=False`，不能伪装成用户消息，也不携带主人身份、最后发言人、参与者列表、个人事实、私聊记忆或 LivingMemory 数据。

## 2. 正式红灯与中断恢复

先新增 `tests/test_proactive_topic.py`，旧代码稳定得到：

- `ModuleNotFoundError: No module named 'astrbot_plugin_shio.core.proactive_topic'`；
- `Ran 1 / FAILED (errors=1)`。

首次实现后的三个 import error 来自隔离容器候选目录被放在错误的 package 层级，不是实现失败；把候选放回 `/tmp/.../astrbot_plugin_shio` 后，同一候选立即进入正式测试。实现测试过程中还发现测试事件使用的 platform/bot 与 canonical group target 不一致，修正测试夹具后没有为生产代码增加兼容旁路。

本阶段在会话中断后从本地代码、远端 staging 和 `SHIO_MASTER_PLAN.md` 重新核对恢复；主计划仍停在 P7-03，未发生越级或假完成。

## 3. 根修

### 3.1 exact topic authority

新增 `ProactiveTopicAuthority`、`ProactiveTopicPlan` 和闭集 `ProactiveTopicSource`。公开构造、copy/deepcopy、跨 authority、跨 Persona、旧 decision replay、计划字段改写与同形对象替换均失败关闭。

同一个 P7-02 decision 只能生成一张计划。P7-01 新 scheduler generation、P7-02 decision 失效、该群出现新消息使 scene revision 更新、Persona interests 改写，都会令旧计划失效。

### 3.2 只使用公共场景与人格兴趣

选择顺序固定为：

1. 在当前群公共 topic 中寻找 Persona interest keyword，按 interest weight、topic revision 和稳定位置选取；
2. 没有公共 topic 命中时，只能退回 Persona 自身公开兴趣 cue；
3. 没有公开兴趣时拒绝签发。

计划只保存 topic 文本、摘要、interest ID、scene revision 和 exact authority lineage。代码与静态测试锁定不读取 `personal_facts`、participants、LivingMemory、sender/principal/owner 或私聊上下文。

### 3.3 canonical scene/persona 快照

`GroupSceneBook.inspect_current_snapshot()` 只接受当前 exact snapshot，并在 scene lock 内重算完整 integrity；旧 revision、copy、字段变异或跨群 snapshot 均拒绝。

Persona 注册和 main 去重均改为 exact object identity，不依赖 dataclass equality/hash；authority 保存公开 interest 的完整 immutable snapshot，每次 inspect 重验。

## 4. 验证证据

- formal red：模块缺失，`Ran 1 / errors=1`；
- P7-01～P7-03 targeted：`25/25`；
- P7 + scene/persona related：`58/58`；
- Windows full：`1149/1149`，skipped 7；
- 隔离 AstrBot Python 3.12 container proactive：`25/25`；
- 隔离 container full：`1149/1149`；
- `compileall`、merge marker、trailing whitespace 与 diff check 通过；
- FNOS/container `/tmp/shio-p703-*` staging 已清理；本地临时 tar 因宿主安全策略拒绝删除，位于 `C:\Users\45928\AppData\Local\Temp\shio-p703-final-20260819.tar`，不在仓库且未部署；
- 生产 `main.py` 仍为 `583bf681d28bbaa22cc705f21136ba52cc06a326d7de7b59a8f559d3d465db5c`，container `running=true / restart=0`，WebUI 200。

候选冻结哈希：

- `core/proactive_topic.py`: `576d48b75a253cef4a2a114ace48daafadb92a4a7863142ce32cc94a6df0e44f`；
- `tests/test_proactive_topic.py`: `45425022d74998db7d3182db1208af20111e9aee3ea186653c05ddb982599213`；
- `core/group_scene.py`: `ac79e2e3cd54a1527a911f4b97ddf1dda9bfc1c4ba449ab323fcc824fc723188`；
- `main.py`: `8ece08d07cd131761a8e6034b10164fda1ef22f9c48be26b3e31944c0d7c87b7`。

## 5. 改动范围

- 新增 `core/proactive_topic.py`；
- 新增 `tests/test_proactive_topic.py`；
- 扩展 `core/group_scene.py` 的 exact current scene inspector；
- 扩展 `main.py` 的 long-lived proactive topic authority 初始化；
- 新增本报告并更新 `SHIO_MASTER_PLAN.md`。

未添加 scheduler task、provider/model 调用或发送分支；未修改第三方插件、AstrBot 核心、FNOS 生产插件或 Git 状态。

## 6. 下一唯一入口

下一唯一入口是 **P7-04 新消息让位、失败退避与主动发送事务**：只允许消费 exact current P7-03 plan；建立真实 scheduler task、generation cancellation、provider 失败退避和防连续独白，并把 proactive 文本接入既有 Persona/Validator/Presentation/Send transaction。默认总开关和空白名单仍必须全关；新群消息必须先取消旧 generation，失败不得连续重试或自说自话。

中断后从本报告与 `SHIO_MASTER_PLAN.md` 第 12 节恢复，不重做 P4～P7-03。P7-04 是第一个可能触及主动模型/发送的阶段，但候选仍先在本地和隔离容器完成；只有 P10 综合生产验收才请用户统一测试效果。
