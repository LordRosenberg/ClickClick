---
name: xiaohongshu-post-comment
description: 在小红书帖子中发表评论。
version: 1.0.0
app: com.xingin.xhs
kind: workflow
capability: post_comment
tags: [comment, input, submit]
source: authored
---

# Post a comment

## Procedure

1. 确认当前是目标帖子详情页，点击右下角评论图标进入评论区。
2. 点击底部输入框。
3. 聚焦输入框后，首先判断是否有未提交的旧输入（浅灰色文本可能只是占位符不是旧输入）：如有需执行`replace_text`；如没有，则直接输入用户要求的评论文本。
4. 使用明确的发送控件提交评论。

## Constraints

- 在小红书帖子详情页发表评论且需要在评论区验证结果时，必须通过右下角评论图标进入评论区，不得使用左下角「说点什么...」入口。 | scope: com.xingin.xhs 帖子详情页发表评论并验证已发表内容时 | evidence: operator-reviewed device traces 2026-08-28

## Verification

- 评论区可以看到刚发表的评论文本，或页面给出明确且与本次提交对应的成功反馈。
- 发表后评论区正常会显示最新发表的内容；如未显示，可点击评论区左上方的“共xx条评论”并选择“最新”后再核对。

## Hints

- 点击评论“发送”后，屏幕会短暂的闪现“评论已发送”的提示。若没采集到，需要从评论区找到该评论。
