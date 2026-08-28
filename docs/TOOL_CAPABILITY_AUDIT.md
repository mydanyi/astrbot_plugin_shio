# P3 工具能力分类审计

审计时间：2026-08-17（Asia/Hong_Kong）

本文件只记录工具名称、注册来源和能力类别，不保存工具调用参数、MCP 地址、认证信息、完整配置或真实聊天内容。

## 1. 当前旧链路

星汐现有权限守卫使用 `guest_allowed_tools` 精确名称白名单：

- 普通群友只保留名称命中白名单的工具。
- 主人保留请求与 AstrBot 全局工具集。
- 主动群聊清空外部工具；表情包工具走独立恢复路径。
- 默认普通群友白名单是 `anysearch_search`、`anysearch_extract`。

名称白名单暂时有效，但不能作为 v2 的最终安全边界：同名工具可能来自不同插件，工具升级后副作用可能变化，未知 MCP 工具也没有统一的能力声明。

AstrBot 自身目前提供 `member/admin` 工具权限，它解决“谁能调用”问题，但不描述工具属于公共读取、文件写入、Shell、设备控制还是完整 Agent，不能替代星汐需要的副作用分类。

## 2. 只读运行盘点

本轮先尝试 `trim-cli`：WSS 5667 无法连接，WS 5666 返回设备错误 `135168`。随后使用既有 SSH 登录态进行只读容器核对：`astrbot` 正在运行，重启计数为 0。

静态注册盘点结果：

| 来源 | 数量 | 说明 |
| --- | ---: | --- |
| AstrBot 内置注册表 | 40 | 从容器内 `iter_builtin_tool_classes()` 读取名称与实现模块 |
| 已安装插件源码 | 10 | AnySearch 3、LivingMemory 2、OmniDraw 3、QQ 管理 1、Meme Manager 1 |
| 动态 MCP | 不固定 | 运行时可新增或改名；P3 分类不能假定一份永久名称表 |

没有通过 Dashboard 导出完整工具 JSON，因为该接口需要认证，且本阶段不需要接触会话 Cookie 或 MCP 配置。动态工具在运行时必须经过同一分类器；无法确认时保持 `unknown`。

## 3. v2 能力类别

| 类别 | 典型工具 | 副作用级别 | P3-01 含义 |
| --- | --- | --- | --- |
| `public_web_read` | AnySearch、AstrBot Web Search、公开网页正文提取 | `public_read` | 读取公开互联网资料 |
| `chat_retrieval` | 知识库查询、长期记忆召回、群消息历史 | `scoped_read` | 读取当前授权范围内的资料或对话 |
| `local_presentation` | `search_memes` | `scoped_read` | 本地表达素材选择，不等于联网或 Agent |
| `media_generation` | 图片、自拍、视频生成 | `state_write` | 产生新媒体或消耗外部生成额度 |
| `memory_write` | 长期记忆写入 | `state_write` | 改变持久记忆状态 |
| `artifact_read` | 文件读取、grep、文件下载 | `scoped_read` | 读取非公开工件；虽然只读，仍不等于普通群友可用 |
| `artifact_write` | 文件写入、编辑、上传 | `state_write` | 改变文件或工件 |
| `shell_exec` | Shell、Python、IPython、Shell Session | `code_execution` | 执行代码或命令 |
| `device_control` | CUA、浏览器控制、发消息、定时任务、群禁言 | `external_control` | 改变外部系统、平台或设备状态 |
| `agent_full` | Skill 候选、发布、回滚和执行历史工作流 | `agent_delegation` | 完整 Agent/委派式工作流 |
| `unknown` | 无声明且无法可靠判断的动态工具 | `unknown` | 失败关闭，不推定为只读 |

“只读”只描述副作用，不直接代表普通群友获准使用。例如文件读取和群历史读取属于 `scoped_read`，但仍可能暴露私有内容；实际授权在 P3-02/P3-03 结合主体、来源和作用域裁决。

## 4. 分类优先级

`core/capability_policy.py` 使用以下顺序：

1. 先检查名称、描述、参数名和实现来源中的高风险语义。发现 Shell、文件写入、设备控制或 Agent 语义时，高风险分类优先。
2. 没有风险冲突时，读取工具显式声明的 `shio_capability` 与 `shio_side_effect`。
3. 再使用本轮审计过的 AstrBot/插件工具目录。
4. 对明显的公共搜索、聊天检索和本地表达工具做保守语义识别。
5. 其余全部归为 `unknown`。

因此，把危险工具改名为 `web_search` 并不能自动获得公共只读分类；测试覆盖了“安全名称 + Shell/删文件描述”的伪装场景。

## 5. 本阶段边界

P3-01 只新增分类模型、审计和测试，没有把它接入现有生产授权裁决：

- `guest_allowed_tools` 的现有行为没有变化。
- 主人、群友和主动群聊当前可见工具没有变化。
- 没有修改 AstrBot、LivingMemory、Meme Manager、OmniDraw、QQ 管理插件或 MCP 配置。
- 没有部署到 FNOS。

P3-02 才会建立 `CapabilityPolicy`，将可信 `PrincipalContext`、聊天场景和上述分类组合成授权结果；切换前继续保留旧名称白名单作为对照。
