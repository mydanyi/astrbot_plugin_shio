# P8-11D New-only 本地完整验收

## 结果

- Python 编译：通过。
- `_conf_schema.json`：解析通过。
- 完整测试：516/516 通过，无 expected failure。
- 本地 pre-model 基准：2000 次，Planner 调用 0，Composer 预算 1，Provider 实际调用 0，p50 0.4332 ms，p95 0.5181 ms，最大 1.0278 ms。

## 旧入口处置

`enabled + all` 下，直接对话无法进入旧 Planner/Replyer；准备失败 fail-closed。旧版未点名自然接话、主动话题和恢复补答生成器暂不启动，避免形成隐藏的旧回复入口。自然称名唤醒保留，因为它进入新版直接对话。

## 部署门

本报告只证明本地代码。线上仍需依次完成只读预检、独立备份、最小部署、配置切换、容器重载、哈希/StartedAt/WebUI/日志验证。测试失败或备份失败时不得部署。
