# P1-04 产品 Trace 合同报告

## 结论

P1-04 已建立独立的闭集产品 Trace 合同。它与现有自由格式 `pipeline_trace` 分开：P1 只供合成评测 runner 使用，不接 `main.py`，因此不改变线上回答行为；P2 起再按真实执行阶段逐步接线。

同时修复两项已经由只读探针稳定复现的旧观测缺陷：

- `sanitize_trace_metadata()` 不再因键名以 `_source`/`_status` 结尾就放行任意 URL 或聊天正文；字符串必须是无空格、无斜杠的闭合 taxonomy 值。
- `final_send_blocked` 现在属于 SEND phase，并以 `send_blocked` 作为终态，不再因之前出现过 `final_reply` 而误报 `ready_to_send`。

## 新合同

- `ProductStage` 固定 ingress、sender/source、memory、media、address、attention、participation、action、evidence、content intent、affect、expression、presentation intent、validation、presentation receipt、send 和 terminal 的顺序。
- `ProductOutcome` 区分 accepted、dropped、no-action、reacted、sent、blocked、cancelled、degraded 和 failed。
- `ProductTracePayload` 只允许闭集状态、稳定 reason code、计数和布尔绑定证据；没有自由字典、正文、身份 ID、URL/path/base64、caption、query、工具参数或工具结果字段。
- conversation revision 和 generation epoch 在 Trace 创建时固定，每个 event 只投影相同版本，不能中途替换。
- sequence、elapsed 和阶段必须单调；重复、倒序、双 terminal、terminal 后追加均失败关闭。
- `SENT` 必须同时存在 presentation intent、真实 presentation receipt 和 send 阶段；只有意图、没有 effect receipt 时不能记为发送成功。

## 验证

- 先建立失败测试，稳定复现缺模块、suffix 泄漏和 blocked 误分类三类红灯；
- 产品 Trace、观测清洗和 metrics 定向测试：`13/13` 通过；
- 既有 pipeline trace、typed runtime、tool result 和 send receipt 回归：`25/25` 通过；
- `compileall` 与相关文件 `git diff --check`：通过；
- P1 汇合后的仓库完整回归：`412/412` 通过；
- 未接入 `main.py`，未修改 FNOS、配置或第三方插件；未部署；未执行 Git/GitHub 写操作。

## 后续接线边界

P2 才能把 Trace 起点前移到任何 name-wake/runtime ingest 之前，并让 banned/self/known-bot/plugin-echo 的零副作用终态可被真实证明。现有 legacy pipeline trace 暂时保留为诊断兼容层，不能再被当作完整产品行为证据。
