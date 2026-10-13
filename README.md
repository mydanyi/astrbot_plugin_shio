<div align="center">

<img src="logo.png" alt="星汐 Shio" width="180">

# 星汐 · Shio

### 给你的机器人完整的一生

从第一次打招呼，到熟悉彼此的日常。

[![版本](https://img.shields.io/badge/Shio-0.5.18-6d7fe8)](https://github.com/mydanyi/astrbot_plugin_shio/releases/latest)
[![AstrBot](https://img.shields.io/badge/AstrBot-4.27.4–4.28.2-52b9b2)](https://github.com/AstrBotDevs/AstrBot)
[![许可](https://img.shields.io/badge/License-MIT-f0bc68)](LICENSE)

[开始使用](#开始使用) · [设置指南](docs/SETTINGS_GUIDE.md) · [常见问题](docs/TROUBLESHOOTING.md) · [下载](https://github.com/mydanyi/astrbot_plugin_shio/releases/latest)

</div>

你给它起了名字，写下性格，也许还为它准备了喜欢的表情包。接下来，希望它能带着这份性格，真正走进你们的聊天。

**星汐是 AstrBot 的自然聊天增强插件。** 它让机器人更懂对话的语境：什么时候接话，什么时候等你说完，怎样用自己的语气回应，又怎样融入一个有很多人的群聊。搭配长期记忆、联网搜索和表情包插件，聊过的事情、共同的兴趣与一次次互动，就能慢慢连成属于这个角色的日常。

## 让设定里的角色，出现在对话里

### 保留它自己的性格

安静、活泼、温柔、嘴硬——角色由你定义。星汐沿用 AstrBot 中的人格设定，用“角色化自然表达”帮助它结合当下情境说话。它不会靠学习群友的口癖来改写自己的性格。

你也可以为主人单独设置称呼和相处方式：一句熟悉的称呼、更亲近的语气，或者只属于你们的互动习惯。主人身份由 AstrBot 识别。

### 懂得什么时候开口

被 @、被引用、被叫到名字时，机器人可以接住对话。没有被点名时，开启“自然参与”，让它结合角色性格和近期聊天内容判断是否适合加入。

你可以调整参与概率、回复冷却和频率，找到适合这个群的存在感。热闹时聊得来，没必要接话时也能安静待着。

### 等你把话说完

“等一下”之后还有补充，“帮我看看”后面还有问题。开启连续消息等待，同一个人短时间内接着发的话可以一起理解。

群友一起闲聊时，可以合并语境再参与；不同人同时 @ 机器人提问时，则分别排队回应。等待窗口和最长等待时间都可以调整。

### 说话有自己的节奏

一句简短回应，或者几条自然衔接的消息，都由你选择。星汐支持单条发送、按句分段和模型分段，气泡数量与间隔按设置控制，代码块也会尽量保持完整。

搭配 Meme Manager，文字之外还可以有贴合语境的表情包。文字气泡和图片各自遵循对应设置。

### 日常闲聊，也认真回应你的托付

星汐为当前对话补充谁在说话、谁被提及，以及这一轮能够使用哪些工具。角色互动可以保留角色的表达；查询信息、发送消息等实际任务，则交给 AstrBot 和已安装的工具处理。

需要进一步约束回复时，可以开启发送前审核，设置自己的基础与附加规则；回复或审核失败时，还可将故障摘要私聊给主人。

## 把日常慢慢补全

星汐负责对话体验，下面这些插件可以为角色带来更多相处的内容。它们均为可选搭配，需要单独安装和配置。

| 搭配插件 | 为角色带来什么 |
| --- | --- |
| [LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) | 保存并召回长期记忆，让聊过的偏好、约定和往事有机会在之后的对话中延续。 |
| [AnySearch](https://github.com/AgIzT/astrbot_plugin_anysearch) | 提供联网搜索工具，陪你了解新话题、查找资料。 |
| [Meme Manager](https://github.com/anka-afk/astrbot_plugin_meme_manager) | 管理和选择表情包，让回应多一点表情与情绪。 |

模型、人格和工具权限统一在 AstrBot 中管理。星汐的可选模块各有开关，可以从简单聊天开始，再一点点调整。

## 开始使用

当前版本 **0.5.18**，支持 **QQ / OneBot v11（aiocqhttp）**，AstrBot 版本范围为 **4.27.4–4.28.2**。

1. 在 AstrBot 中接好 QQ、聊天模型与人格，确认基础对话正常。
2. 打开插件管理，通过仓库地址安装：`https://github.com/mydanyi/astrbot_plugin_shio`。也可以从 [Releases](https://github.com/mydanyi/astrbot_plugin_shio/releases/latest) 下载 ZIP 安装包。
3. 在星汐的“聊天范围”中配置聊天范围：私聊 QQ 名单与群号名单留空时，Shio 不限制对应的聊天范围；填写后，普通用户只允许进入名单内的私聊或群。主人身份沿用 AstrBot 官方设置。
4. 想让它参与群聊，开启“允许自然参与”并设置参与群号；想加强角色表达，开启“角色化自然表达”。
5. 选择喜欢的文字气泡方式。**由星汐控制分段时，请关闭 AstrBot 自带分段**，避免同一条回复被拆两次。

所有辅助模型都从 AstrBot 已有模型中下拉选择。初次使用可以先选“按句分段”，它不需要额外的分段模型调用。更多配置见[设置指南](docs/SETTINGS_GUIDE.md)。

## 使用前了解这几件事

- 自然参与概率作用于“判断适合接话”之后，设为 100% 也不代表每句话都回复。
- 自然参与由真实群消息触发；当前版本不包含无人发言时主动开启新话题的功能。
- 审核、智能判断、模型分段和回复准备会增加相应模型调用与等待时间，可按需开启。
- 聊天及辅助判断会将相应内容交给你选择的模型服务。使用前请确认服务的数据处理方式，群聊中也应让参与者知情。

## 继续了解

- [安装、升级与回退](docs/INSTALLATION_AND_ROLLBACK.md)
- [逐项设置指南](docs/SETTINGS_GUIDE.md)
- [常见问题与排查](docs/TROUBLESHOOTING.md)
- [平台与插件兼容说明](docs/COMPATIBILITY.md)
- [更新记录](CHANGELOG.md)

欢迎在 [Issues](https://github.com/mydanyi/astrbot_plugin_shio/issues) 分享使用体验、报告问题或提出想法。反馈时请附上版本、相关设置和复现方式，并遮住密钥与私人聊天内容。

感谢 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 及生态插件作者，让这些日常有了发生的地方。星汐使用 [MIT License](LICENSE) 开源；开发者可阅读[架构说明](docs/ARCHITECTURE.md)。
