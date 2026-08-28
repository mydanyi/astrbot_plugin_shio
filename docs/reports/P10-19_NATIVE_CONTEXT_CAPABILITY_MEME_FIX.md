# P10-19 原生上下文、真实能力与语义选图修复

## 结论

0.5.19 修复的是插件链主因，不以更换强模型遮蔽问题：群聊历史重新以 provider 原生 messages 进入普通／自然／冷场首稿和 repair；能力问答只认代码生成的本轮真实能力；validator 能拒绝“复述问题但没回答”等事故输出；Meme 对齐只修改 Shio，不修改或部署第三方 Meme Manager。

## 根因与修复

### 1. 上下文存在但不可用

旧链路能够从 ledger 读到多条历史，却把说话顺序、Persona、情绪、事实和合同压成大型单条 user JSON。0.5.19 新增 canonical model message 投影：只接纳同 scope、已验证、早于当前消息的公开 user 历史与精确可信 assistant 输出，保留真实 role 和顺序；当前消息不会重复进入历史。普通／自然首次生成和 repair 复用 exact contexts，冷场主动首稿与唯一 repair 同样复用。

### 2. 配置能力冒充本轮能力

新增 `CapabilitySnapshot`，只根据 typed policy 与权限过滤后的有效工具名称生成知识库、公网、外部动作和 Agent 状态。Persona 只能改变表达，不能声明未接入能力。

### 3. 有关键词但没有回答

新增回答义务反证：问题复述、新实体澄清漂移、能力回避、Persona 幻想覆盖现实物理边界均拒绝；每类同时保留相邻自然短答，避免把修复从误放变成误拦。

### 4. repair 丢失可信 system 约束

自审发现 Persona 和能力迁入 system role 后，repair 仍只携带 user payload。0.5.19 的 repair system 现在 exact 继承首次生成 system prompt，并继续使用相同原生 contexts、媒体和语义合同。这是本轮全量复审发现并修复的真实二次缺陷。

### 5. Manager 读取了未校验首稿，主动链又绕过正常钩子

官方 Meme Manager 4.15.1 的响应钩子优先级是 99999，旧 Shio 回复审查只有 100，Manager 因而先从可能被拒绝／修复的首稿选图。0.5.19 把 Shio 最终审查提升到 `sys.maxsize`，使 Manager 的正常响应钩子只能读取最终可见回复；其装饰与发送钩子仍保持原样，Shio 气泡分发保留非文本图片组件。

主动／冷场来自独立调度器，没有 AstrBot 原生 LLM event，不能假装已进入上述逐图语义钩子。它在全部文字真实发送成功后，用最终可见回复和最近公开群聊从官方 22 类中做情境选择，再只调用官方 `compat_prepare_message` 与 `compat_send_prepared_message`。这是公开兼容路径，不冒充逐图向量语义路径；Manager 仍独占概率、资源包、图片构建和发送。

## 配置与所有权

- Shio WebUI：7 组、56 项；删除三个重复 Meme 字段。
- Meme 开关、概率、资源包和语义模型：只在 Meme Manager 设置；本发布不修改其代码、配置或运行目录。
- `replyer_provider_id`：只控制冷场主动与 repair；普通／自然首稿沿用 AstrBot 当前 provider。
- 跨插件合同：Shio 校验 Manager 4.15.1 的唯一运行实例、公开异步方法签名和绑定；生产 4.15.1 与本地副本整文件 hash 不同时也不要求替换第三方，且不存在自造语义方法。

## 审查证据

- Shio 最终全量：1339/1339（skipped 7）。
- 固定矩阵：24/24；Persona A/B：6/6；多模态：29/29；行为矩阵：24/24。
- 候选包专项：5/5；compileall 与 diff check 通过。
- 回归覆盖官方钩子顺序、正常链无重复补图、Manager 工具／prompt 保留但不冒充用户检索能力、最终 marker 交还、repair 丢弃旧 marker、主动链只走公开接口。Meme Manager 仓库保持 clean。
- 首轮全量曾暴露 23 failures + 4 errors：1 项为 repair system 约束真实回归，其余按产品合同更新为 provider role／跨插件公开接口／新版本候选断言；没有删除生产安全门。

## 候选与部署边界

唯一候选为 `astrbot_plugin_shio_v0.5.19_upload.zip`：100 文件、548819 bytes、SHA-256 `fea1de957e3e45c0f5b04783386cfa5f98189af03feff65532480917667c5724`；manifest SHA-256 `8cebddd4007d24217ecfe8c792d7e4dce90572c98420315fc35e5b2981095015`。部署目录、备份、manifest 与重启事务都只指向 Shio；没有构建、上传或部署 Meme Manager 候选。

## 部署与运行态证据

- 事务备份：`/AstrBot/data/backups/shio/P10-19-official-meme-alignment-20260824T100023Z`；最终阶段 `VERIFIED`。
- Shio 0.5.19 已加载；metadata/main/schema SHA-256 分别为 `b0e48f7fb039fe867ce8ab9ef5537116edfa697213eb898b20d12664e9f1c8e2`、`e236a147e9aa6d283a4f453329ffdf9627b0ab450f8d5c68627bc51cacbd515b`、`718773e600f3971ffa0750450bc9a3ebe0400bd0fae68b69ed310d1ed4d23f23`。
- Manager 仍为 4.15.1；metadata/main/handler 的部署前后 hash 完全一致，部署脚本没有写入其代码、配置或运行目录。
- Shio 配置从 59 项迁移为 56 项，只移除三个重复的 Manager-owned 字段，其余既有值逐项保持。
- 容器 running=true、RestartCount=0、OOM=false，WebUI 200；新启动窗口 ERROR/Traceback/CRITICAL 均为 0，主动调度器启动成功。

## 尚未完成的证据层

真实群的可见语义质量仍由用户消息验收：普通／自然回复应由 Manager 正常语义工具按 Shio 最终校验文本选图；主动／冷场应按公开类别兼容路径产生情境相符的图片回执。部署健康、测试全绿和接口可达都不能代替这层确认。
