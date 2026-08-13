---
name: distill-learning-dialogue
description: Distill a completed learning conversation or a user-supplied learning transcript into a concise Markdown draft that preserves the learner's initial confusion, exploration path, question chain, simple answers, evidence, conclusions, cognitive changes, and unresolved items, then save it in the repository's top-level draft directory for later writing. Use when the user explicitly invokes this skill or explicitly asks to “沉淀本次学习过程”, “整理疑问到 draft”, or otherwise save a completed learning dialogue as an intermediate draft. Do not use for ordinary conversation summaries, meeting notes, polished articles, or new research.
---

# Distill Learning Dialogue

将一次学习对话压缩成可追溯的中间稿。保留“为什么不理解—如何探索—问题如何衍生—最终得到什么”，不把它改写成正式文章。

## 确定输入范围

1. 默认使用当前对话截至技能调用时可见的内容。
2. 用户提供对话转录或指定本地材料时，将它们与当前对话一起作为输入。
3. 如果当前上下文不包含实际学习过程，请用户提供原对话或材料，不创建空洞草稿。
4. 如果可见上下文显然不完整，在“对话范围”中说明实际覆盖边界，不猜测看不到的轮次。

## 提炼学习轨迹

1. 识别学习目标、已有背景和触发这次探索的事件。
2. 收集用户明确表达的困惑和问题。可以记录有充分上下文依据的潜在困惑，但必须标注为“推测”。
3. 按实际顺序还原主要探索过程：阅读了什么、尝试了什么、哪些假设被验证或否定、理解如何被修正。
4. 为每个核心问题提供一个尽量简单的答案，并仅使用以下状态：
   - `已解决`
   - `部分解决`
   - `未解决`
5. 对对话中没有得出答案的问题，将简短答案写为“尚未得出”，不自行补全。
6. 收集由原始问题引出的衍生问题，说明它与哪个原始问题相关，并保留它的当前状态。
7. 提取最终结论、认知变化、仍待探索的问题和可能的后续写作线索。
8. 删除寒暄、重复说明、工具执行日志和与学习无关的内容，但不删除改变结论的失败尝试或理解转折。

## 限制边界

- 仅根据选定对话和已提供材料提炼，不主动搜索网络、查找新资料或扩展研究。
- 不将普通对话摘要、会议纪要或正式文章写作伪装成学习草稿。
- 不把 AI 推断的学习动机、情绪或理解障碍写成用户的明确陈述。
- 不自动将草稿改写到 `output/`，也不删除、更名或移动旧草稿。

## 保存草稿

1. 定位当前仓库根目录下的 `draft/`。只在该目录中创建一个 Markdown 文件，不创建下级目录。
2. 命名为 `YYYY-MM-DD-<topic-slug>.md`。`topic-slug` 使用反映中心主题的小写 kebab-case，避免空格和“学习笔记”之类空泛名称。
3. 如果目标名已存在，使用 `-02`、`-03` 等后缀，不覆盖现有文件。只有用户明确指定要更新的草稿时才原地修改。
4. 在“主要参考资料”中保留对话已使用的 URL。对本地资料使用从草稿文件出发可解析的相对路径；对子模块记录仓库名和 commit。
5. 使用以下固定结构：

```markdown
# 学习主题

- 日期：YYYY-MM-DD
- 状态：中间稿
- 对话范围：
- 主要参考资料：

## 学习起点

## 最初的困惑

## 探索过程

## 核心问题与简答

### Q1. 问题

- 为什么会产生这个问题：
- 状态：已解决 / 部分解决 / 未解决
- 简短答案：
- 依据：

## 衍生问题

## 认知变化与关键结论

## 仍未解决的问题

## 后续写作线索
```

## 验证与交付

1. 重新读取生成的草稿，确认所有固定章节都存在且没有遗留占位符。
2. 确认每个核心问题都有状态、简短答案和依据；未解决项明确写为“尚未得出”。
3. 确认所有 AI 对困惑原因的判断都标注为“推测”，且没有添加对话外的事实或结论。
4. 确认本地引用可从草稿位置解析，且未覆盖现有草稿。
5. 返回生成的 Markdown 文件可点击路径，简要说明记录了哪些已解决、部分解决和未解决项。
