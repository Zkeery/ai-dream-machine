# AI造梦机 · Tool / Skill 清单

> 更新：2026-10-04  
> 说明：确定性工具承载执行，单阶段Agent只使用受限工具。表中「Tool」为编排可调用的能力单元，不是向用户开放任意工具调用。

## 1. Tool list

| tool_id | 名称 | 输入 | 输出 | 副作用 | 超时/重试 | 人工确认 |
| --- | --- | --- | --- | --- | --- | --- |
| llm.chat_structured | 结构化文本生成 | prompt + schema | JSON 对象 | 计费 | 有限重试；4xx 不盲重试 | 否（由阶段按钮触发） |
| image.t2i | 文生图 | prompt | 图片文件 | 计费+落盘 | 有限重试 | 否 |
| image.ref | 参考约束生图 | prompt+refs | 图片文件 | 计费+落盘 | 有限重试 | 否 |
| video.i2v | 首帧/首尾帧生视频 | 实际帧图+单镜头提示+所选模型 | 视频文件 | 计费+落盘 | GET有限重试；POST未知不重发 | 随用户生成动作 |
| video.r2v | 参考生视频 | 分镜图/角色图/场景图+单镜头提示+所选模型 | 视频文件 | 计费+落盘 | 同上；超图数/规格先拒绝 | 随用户生成动作 |
| tts.speak | 口播 / 漫剧角色语音 | 文本+声音设置 | 音频 | 在线调用+落盘 | 失败可读 | 否 |
| ffmpeg.concat | 格式统一后拼接 | 片段列表 | mp4，原片保留 | CPU+临时文件+落盘 | 失败可读，临时文件清理 | 否 |
| ffmpeg.comic | 漫剧合成 | 镜头图+对白语音+字幕 | 基础运镜 MP4 | CPU+落盘 | 失败可读 | 否 |
| safety.prompt_check | 提示词拦截 | 文本 | pass/block | 无 | 本地规则 | 否 |
| safety.result_check | 结果拦截 | 文本/元数据 | pass/block | 可能丢弃结果 | 本地规则 | 否 |
| fs.save_artifact | 产物落盘 | bytes+meta | path | 写磁盘 | — | 否 |
| db.execution_ledger | 执行账本 | 状态事件 | 可订阅进度 | 写 SQLite | — | 否 |
| auth.validate | 鉴权 | token | user_id | 无 | — | 否 |
| assets.reuse | 素材复用 | asset_id | upload 记录 | 写上传索引 | 归属校验 | 用户选择素材 |
| knowledge.retrieve | 创作资料检索 | 本人选定库+查询 | 固定约束+来源版本快照 | 读本地索引/保存快照 | 故障明确报错 | 随用户生成动作 |
| models.resolve | 解析生成模型 | 项目/任务选择+模式 | 已开放兼容模型 | 保存执行快照 | 非法选择明确拒绝 | 用户选择 |

## 2. Skill list（产品技能包，面向编排）

| skill_id | 描述 | 组合的 tools |
| --- | --- | --- |
| skill.story.script | 剧本阶段 | knowledge + llm + safety |
| skill.story.characters | 角色场景 | llm + image + safety |
| skill.story.storyboard | 分镜 | knowledge + llm |
| skill.story.reference | 参考图 | image.ref |
| skill.story.video | 视频段 | video.* |
| skill.story.post | 成片 | ffmpeg |
| skill.comic.script | 漫剧剧本 | knowledge + llm + safety |
| skill.comic.characters | 漫剧角色场景 | image + safety |
| skill.comic.storyboard | 漫剧镜头对白 | knowledge + llm |
| skill.comic.panels | 漫画镜头图 | image.ref |
| skill.comic.audio | 角色对白配音 | tts |
| skill.comic.composition | 漫剧成片 | ffmpeg.comic |
| skill.short.literary | 文艺短视频 | llm + image + tts + ffmpeg |
| skill.short.motion | 动作短片 | video.r2v (+ffmpeg) |
| skill.short.talking | 口播 | tts + ffmpeg |
| skill.recovery | 中断恢复 | db.execution_ledger |

本表是能力索引，`tool_id` / `skill_id` 不是新增的对外 API。故事与漫剧共用知识库基础设施、鉴权和模型客户端，但阶段与产物依赖分开。文本和图片生成按本次选择调用；TTS 通过在线语音服务执行，FFmpeg 合成在本机执行。

## 3. 禁止的 Tool 行为

2026-10-05新增视频适配为Seedance2.5和Veo3.1，仍属于原video工具，不新增外部网关或独立Agent。能力/时长/费用由服务端确定性合同约束；Agent不能选择未开放模型、绕过图数限制或把整部故事追加到每个片段。后期工具不支持自动配乐、调色或叙事重剪。

1. 工具不得自行决定「再跑一遍收费生成」。  
2. 工具不得接受客户端任意文件系统路径。  
3. 工具不得把密钥写入产物或日志。
