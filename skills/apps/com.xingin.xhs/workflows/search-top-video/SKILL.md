---
name: xiaohongshu-search-and-sort
description: 对小红书的搜索结果按照热度/类型/时间/距离等过滤/排序。
version: 1.0.0
app: com.xingin.xhs
interface_scope: app
kind: workflow
capability: search_and_sort
tags: [search, video, likes, filter]
source: authored
---

# Search and Sort

## Procedure

1. 如果当前结果页的搜索框已经显示目标词，保留该查询并继续；否则打开搜索入口，聚焦搜索框，输入目标词并显式提交搜索。
2. 到达结果页后，打开表示当前排序或筛选状态的入口。当前版本的唯一入口在「全部」Tab（同时有**三条横线**标识），点击该入口应该会出现下拉菜单。
3. 在下拉菜单中按需选择，如“最热“可选「最多点赞」。**注意**所需选项要在“收起”下拉菜单前设置完。
4. 收起选项后，按需在结果页选择目标。

## Constraints

- 小红书搜索结果页的筛选/排序下拉入口只能从带三条横线标识的「全部」Tab 打开；「综合」等排序文字不是该入口。 | scope: com.xingin.xhs 搜索结果页需要打开筛选或排序下拉菜单时 | evidence: operator-reviewed device traces 2026-08-28

## Verification

- 在下拉菜单中选择了所需的过滤条件，并在过滤结果页选择最符合要求的目标。

## Hints

- 小红书的过滤结果不保证全局严格降序；目标是在已应用条件的结果页中比较可见点赞数，而不是证明全站绝对最大值。
