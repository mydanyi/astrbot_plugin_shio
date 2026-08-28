# P8-11C 工具、引用与多模态边界

## 工具

- 主人必须由可信 sender ID 命中 owner allowlist，才保留完整工具集。
- 群友只保留管理员精确配置、能力分类为聊天相关只读/本地展示、且来源通过审计的工具。
- 工具参数永不进入 typed result；结果必须同时匹配本轮 `scope_key` 与 `target_sender_key`，成功后才可成为输出校验事实。

## 引用与附件

- 当前消息始终是 ReplyTarget；被引用消息单独放入 `explicit_reference`，包含独立 message/sender 归属，不提升权限。
- 引用正文只从同 scope 的 ledger 中按 message ID 精确匹配。
- 图片和音频 URL 继续交给当前 Provider；Composer 只看到附件数量说明，不能编造未收到的内容。

## 角色和语言

首选语言仍由可替换 PersonaPackage 决定；除翻译、外语练习或明确指定外语外保持中文。群友关系边界与工具能力分离，主人专属关系不会因工具、昵称或引用继承。
