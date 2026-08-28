# P4-02 Persona facts、价值/兴趣与关系动作热路径报告

## 结论

P4-02 已完成本地与隔离 AstrBot Linux container 门，候选未部署。

最终 Persona Renderer 现在从同一 `PersonaPackage` 读取以下代码校验后的表达数据：

- `core_traits` 被投影为带稳定 ID、指导文本和权重的 `value_guides`；
- `character_facts` 被投影为带稳定 ID、正文和 `source_kind` 的角色事实，其中包含角色兴趣与偏好；
- 当前可信关系距离对应的 `allowed_action_ids` / `forbidden_action_ids`、称呼、边界与 warmth；
- 当前情绪轨迹和候选表达素材。

这些数据只进入最终可见文本渲染层，不修改 `ContentIntent`、Principal、Capability、ReplyTarget、工具、动作或发送 authority。

## 根因

P4-01 冻结基线中的 Composer 只输入了身份摘要、性格描述、语言偏好、关系称呼/边界和情绪轨迹。两个明确缺口是：

1. `character_facts` 完全没有进入 Prompt，亚托莉的原作事实、兴趣和边界事实无法稳定参与相关回答；
2. `PersonaExpressionPlan` 虽已有关系允许/禁止动作，但 Composer 没有输出这些字段，也没有重验计划与当前 Persona 关系规则完全一致。

因此资产层已经拥有的数据没有真正进入热路径；同关系距离下伪造 allowed/forbidden 动作也能通过旧 Composer 构造边界。

## 改动

- `core/reply_composer.py`
  - 新增严格的 Persona prompt source 投影；
  - 重验当前 PersonaPackage 安全报告；
  - 对本层使用的 package、trait、fact、language、relationship 和 action tuple 做 exact 类型检查；
  - 要求 `PersonaExpressionPlan` 的称呼、边界、warmth、allowed/forbidden 与当前关系规则完全一致；
  - Prompt 明示角色事实只在当前问题相关时使用，不能编造未提供经历；
  - Prompt 明示关系动作仅为表达边界，不能授予工具、权限、目标或发送能力。
- `tests/test_reply_composer.py`
  - 增加 facts/价值/兴趣进入 Prompt；
  - 增加 peer 与 primary-bond exact 关系动作进入 Prompt；
  - 增加同距离伪造关系动作在生成前拒绝；
  - Owner fixture 只使用既有 code-owned owner policy builder。

## 红灯与绿灯

首轮正式红测为 `3/3` 失败：

- 缺少 `value_guides` / `character_facts`；
- 缺少 relationship action allow/forbid；
- 伪造 peer 关系动作未被拒绝。

根修后：

- P4-02 新定向：`3/3`；
- Persona/关系/Composer/Pipeline related：`136/136`；
- Windows full：`1064/1064`，skipped 7；
- AstrBot Python 3.12 隔离 container full：`1064/1064`；
- `py_compile` 与 `git diff --check`：通过。

## 生产边界

- 未覆盖线上插件，未重启生产容器；
- 线上 `main.py` SHA256 仍为 `583BF681D28BBAA22CC705F21136BA52CC06A326D7DE7B59A8F559D3D465DB5C`；
- AstrBot 仍为 running、restart=0、WebUI 200；
- production adapter allowlist 和 live output authority 仍未开放，四类 owner adapter 继续零执行；
- 未执行 Git 写操作。

## 风险与下一入口

P4-02 只解决 Persona 内容和当前关系动作进入热路径。连续 `AffectStateBook` 的历史状态仍未参与最终 Persona 表达；同一 ContentIntent 在不同 Persona 下的事实一致性和表达差异仍需专门验收。

唯一下一入口是 **P4-03 Persona Renderer**：把当前轮 appraisal 与 P4-01 continuous AffectState 投影为 renderer-only typed state，并验证同一 ContentIntent / target / authority 在不同 Persona 下保持事实骨架一致、只改变表达。
