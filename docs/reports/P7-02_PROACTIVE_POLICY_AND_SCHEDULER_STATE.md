# P7-02 proactive policy 与 scheduler state 报告

## 1. 结论

P7-02 已完成；候选未部署，主动生成与发送仍未启用。

星汐现在有独立、持久、默认全关的 proactive policy state。只有显式总开关为 true、群 ID 在非空白名单、时段/观察期/空闲/冷却/日限额全部满足、P7-01 candidate generation 仍 current、状态文件与本机 secret 完整匹配时，才会签 `ADMITTED` decision。

即使 `ADMITTED`，本阶段仍固定 `model_authorized=False`、`send_authorized=False`；P7-03 必须先选择仅含群公共信息的主题，P7-04 才能建立 scheduler task 和发送事务。

## 2. 正式红灯

先新增 `tests/test_proactive_policy.py`，旧代码稳定得到：

- `ModuleNotFoundError: No module named 'astrbot_plugin_shio.core.proactive_policy'`；
- `Ran 1 / FAILED (errors=1)`。

这证明 P7-01 只有 typed source，尚无白名单、时段、冷却、日限额或重启防重复状态。

## 3. 根修

### 3.1 默认全关配置

`_conf_schema.json` 新增 proactive 总开关、群白名单、活跃起止小时、时区、观察期、空闲、冷却和每日上限。总开关默认 false，白名单默认空；与自然称名白名单不同，proactive 空白名单永远表示“没有群可主动发起”，不会解释成全部群。

配置冻结为 exact `ProactivePolicyConfig`；bool/int/tuple 类型、范围、重复群 ID 或 malformed 值不合法时，main 回落到默认全关，而不是容错放宽。

### 3.2 持久 scheduler state

`ProactivePolicyState` 按群保存：首次观察、最后已准入人类活动、最后 scheduler 检查、最后 admission、当地日期与当日次数、最后已消费 observation。候选 admission 先通过临时文件 flush/fsync + `os.replace` 落盘，成功后才发布 decision；写盘失败只会得到 `STATE_UNAVAILABLE`，不会继续模型或发送。

重启时 normal state 保留冷却、日限额和 observation replay fence。同群旧 P7-01 generation 不能进入策略；墙钟倒退也失败关闭。

### 3.3 隐私与 secret 绑定

落盘不保存 platform/bot/group/user/message/content，只保存由 32-byte 本机 secret 域分离 HMAC 得到的群指纹和时间/计数闭集。完整 records list 另有 HMAC；secret 缺失、替换、state MAC 不匹配、重复 JSON key、未知 schema、超限或损坏均使 store 全关。

main 只在 inbound 已经通过 admission、group scene 与 address 安全门之后记录该群活动时间。默认配置不会创建 `proactive/` 目录；banned/self/bot/plugin/被拒事件不能借此更新 scheduler state。

## 4. 验证证据

- formal red：模块缺失，`Ran 1 / errors=1`；
- P7-02 targeted：`10/10`；
- P7-01 + P7-02：`18/18`；
- proactive/P5/runtime/core related：`101/101`；
- Windows full：`1142/1142`，skipped 7；
- 隔离 AstrBot Python 3.12 container proactive：`18/18`；
- 隔离 container full：`1142/1142`；
- `py_compile`、`compileall`、merge marker、trailing whitespace 与 diff check 通过；
- FNOS/container `/tmp/shio-p702-*` staging 已清理；
- 生产 `main.py` 仍为 `583bf681d28bbaa22cc705f21136ba52cc06a326d7de7b59a8f559d3d465db5c`，container `running=true / restart=0`，WebUI 200。

全仓首次运行的唯一失败来自 unittest discovery 同时以顶层名和 package 名加载 `test_pipeline`，导致测试夹具写到了另一份 `FakeStarTools`。按仓库既有兼容导入方式修正测试后，原命令复跑 `1142/1142`；实现没有为此增加兼容旁路。

候选冻结哈希：

- `core/proactive_policy.py`: `d52416315f3fe75a14e55f5b376f7acfcf7b898b0dd9632288f5e8e12a308800`；
- `tests/test_proactive_policy.py`: `5eb94216cbee5ba751be29450368b9767e890f31b6f99f63fc028c5c94097a40`；
- `main.py`: `3e7f9c44d38655d990bce4581b562592840260456d11ef9a4108bc005460dc93`；
- `_conf_schema.json`: `1ee4948d682849642b7a7cb3ad3001af1318a05ed466e1c951678ec44655e6e1`。

本地 staging archive `C:\Users\45928\AppData\Local\Temp\shio-p702-candidate-20260819a.tar` 不在仓库、FNOS 或容器内，也未部署。

## 5. 改动范围

- 新增 `core/proactive_policy.py`；
- 新增 `tests/test_proactive_policy.py`；
- 扩展 `main.py` 的 long-lived authority/state 初始化与 accepted group activity observation；
- 扩展 `_conf_schema.json`；
- 新增本报告并更新 `SHIO_MASTER_PLAN.md`。

未添加 scheduler task、模型调用、topic selection、发送分支；未修改第三方插件、AstrBot 核心、FNOS 生产插件或 Git 状态。

## 6. 下一唯一入口

下一唯一入口是 **P7-03 公共话题与人格兴趣选择**：只允许 P7-02 exact `ADMITTED` decision，输入只能是 canonical group public scene/topic 与 Persona 公共兴趣；个人事实、主人私聊、LivingMemory 私有召回、最后说话者身份和模型自由建议不得进入候选。输出仍是 typed topic plan，零模型零发送。

中断后从本报告与 `SHIO_MASTER_PLAN.md` 第 12 节恢复，不重做 P4～P7-02。只有 P10 综合生产验收才请用户统一测试效果。
