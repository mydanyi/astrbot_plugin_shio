# P8-01 群公共、个人与主人私有记忆策略

## 结论

P8-01 已完成本地与隔离 AstrBot Linux 容器候选验证，候选未部署。

星汐的 LivingMemory 消费层现在把记忆严格分为 `group/group_background/global/public` 公共类、`personal` 当前主体个人类、`owner_private` 主人私有类。主人私有事实只有在当前 accepted human 同时是可信主人、当前会话是私聊、事实主体与当前 sender key 完全一致时才可进入本轮上下文；群聊、普通用户、其他主体、无主体私有事实全部 fail closed。

当前消息仍是不可改写的语义骨架。记忆只进入 `screened_context_facts` 低优先级背景，不会新增 ContentIntent required atoms，也不能替换当前 sender、target、question anchor 或 answer language。

## 正式红灯

新增 `tests/test_p8_memory_scopes.py` 后，首轮因 `MemoryScope` 和主动公共投影尚不存在而在导入阶段失败：`ImportError: cannot import name 'MemoryScope'`。这证明原实现只有“当前主体/公共背景”两个松散桶，没有 P8 所要求的第三类主人私有边界。

完整发现首轮另暴露测试模块相对导入不适用于 discovery；改为包绝对导入后才进入全量门。这是测试装载问题，不通过兼容旁路掩盖。

## 实现

### 1. 闭集 scope

- `MemoryScope` 明确 `GROUP`、`GROUP_BACKGROUND`、`GLOBAL`、`PUBLIC`、`PERSONAL`、`OWNER_PRIVATE`。
- LivingMemory adapter 只保留 subject fact 的 `personal` 或 `owner_private`；其他 subject scope 直接排除。
- `MemoryDecision` 与 `MemoryPolicyResult` 双层验证 subject/public scope，避免错误 shape 越过选择器。

### 2. 主人私有门

- 非主人携带 `owner_private`：`owner_private_nonowner`。
- 即使是可信主人，只要当前是群聊：`owner_private_group_forbidden`。
- 即使 scope 正确，只要 subject 不是当前 sender：`other_subject`。
- 只有可信主人私聊的当前主体事实可进入 `owner_private_facts`。

关系角色只控制记忆可见性，不能提升工具权限；主人身份仍来自已冻结的可信 Principal。

### 3. 当前消息优先与主动轮边界

`context_order` 固定从 `current_message` 开始，其后才是 personal、owner-private、group-public。ReplyComposer 现有系统约束继续声明记忆只能补充背景，不能替换 current anchor/message/content intent。

新增 `proactive_group_public_facts()` 作为未来主动轮唯一合法记忆投影：只有 group turn 的无主体公共 facts 可返回；私聊结果、personal 与 owner-private 一律不可投影。P7 主动运行时当前仍保持零记忆，因此本层没有扩大线上主动发言输入。

## 测试证据

- P8-01 + MemoryPolicy + ContextAssembler + ContentIntent：`44/44`。
- Windows 完整发现：`1164/1164`，skipped 7。
- 隔离 AstrBot Linux container 定向：`44/44`。
- 隔离 AstrBot Linux container 完整发现：`1164/1164`。
- `compileall`、schema JSON、`git diff --check`：通过。
- 静态核对 P7 proactive runtime/topic 不导入 LivingMemory 或 personal/owner-private 数据。

## 冻结哈希

- `core/memory_policy.py`: `4BFB6171D82965F34F2840A99EC96CCD7C363687011CC5A03A7AB8E236C12E64`
- `core/plugin_adapters/livingmemory.py`: `12D89AC89EB52A7B2A3095B3B2F772AC2EDD29B4ACCFC4FF66B48E2B78C97760`
- `core/contracts/context.py`: `5BE993BCC1E8C818E6D56EA5BB6BBD530BF49A1B62DE939718D0C026DC0BF728`
- `tests/test_p8_memory_scopes.py`: `2D1BED81AE795F90054DA82E39A26F7157E8279CAFBABEA41ECEAD7F646EFE8F`
- `tests/test_content_intent_builder.py`: `D22A9F075C679B8A2855853932E5716FBF8EBCC6DBF08A04B86FE52E257EC426`

## 生产与边界

- 未修改 LivingMemory、AstrBot core、Meme Manager 或其他第三方插件。
- 未部署、未重载、未重启生产容器；线上 `main.py` 仍为 `583BF681…DB5C`，container running、restart 0、WebUI 200。
- 已删除 FNOS host 与容器内的 P8-01 精确临时 staging；Windows 本地临时 tar 因安全策略保留，不在插件扫描路径。
- `X-01` 不变：本层承诺星汐零错误消费，不冒充 LivingMemory 被动捕获零存储。
- 未执行 Git 写操作。

## 下一入口

唯一下一入口是 **P8-02 关系状态由具体事件更新**：从 accepted inbound、exact send receipt 和可信 feedback 建立可解释的关系事件账本；关系变化只影响表达距离，绝不能改变 Principal 或 CapabilityPolicy。中断后从本报告和 `SHIO_MASTER_PLAN.md` 第 12 节恢复。
