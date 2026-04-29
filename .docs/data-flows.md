# 数据流概览

## 微博抓取流程

```
parse_uid(url)                    # 解析 URL → UID
  ↓
_fetch_weibo_cards(uid)           # 请求 m.weibo.cn API 获取卡片列表
  ↓
_extract_valid_mblogs(cards)      # 提取有效博文，过滤置顶（isTop, is_top, title 等）
  ↓
check_weibo(uid)                  # 比较 last_id，筛选新微博
  ↓
_collect_new_posts(posts)         # 应用屏蔽词/白名单/原创转发过滤
  ↓
_send_new_posts(posts, targets)   # 推送到目标会话
```

## 热搜抓取流程

```
_fetch_hotsearch()                # 先尝试无 Cookie 请求 weibo.com/ajax/side/hotSearch
  ↓ (失败时)
带 Cookie 重试                    # Cookie 兜底策略
  ↓
过滤广告位 (is_ad, is_ad_pos)    # 可选
  ↓
_push_hotsearch(items, targets)   # 格式化并推送到目标会话
```

## 每日总结流程

```
读取昨日日志文件 (logs/YYYYMMDD.log)
  ↓
统计各用户推送次数 + 热搜推送次数
  ↓
格式化总结消息
  ↓
推送到所有目标会话
```
