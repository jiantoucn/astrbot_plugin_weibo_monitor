# main.py 代码行号索引（共 1468 行）

> 基于当前代码的精确行号，方便快速定位功能。
> 代码变更后必须同步更新此索引。

## 文件头部（L1 ~ L31）

| 行号范围 | 内容 | 说明 |
|----------|------|------|
| L1 ~ L12 | `import` 语句 | 标准库导入：`asyncio`, `re`, `httpx`, `os`, `json`, `base64`, `random`, `logging`, `RotatingFileHandler`, `datetime/timedelta/timezone`, `typing`, `functools`, `urllib.parse` |
| L14 ~ L16 | AstrBot 框架导入 | `filter`, `AstrMessageEvent`, `MessageChain`, `Context`, `Star`, `register`, `StarTools` |
| L17 | `BeautifulSoup` 导入 | HTML 解析库 |
| L19 ~ L31 | 常量定义 | `DEFAULT_CHECK_INTERVAL`(10), `DEFAULT_REQUEST_INTERVAL`(5), `DEFAULT_TIMEOUT`(20), `MAX_CONCURRENT_REQUESTS`(5), `DEFAULT_MESSAGE_TEMPLATE`, `WEIBO_API_BASE`, `WEIBO_MOBILE_BASE`, `WEIBO_WEB_BASE`, `HOTSEARCH_API_URL`, `DEFAULT_HOTSEARCH_INTERVAL`(60), `DEFAULT_HOTSEARCH_TOP_N`(10), `DEFAULT_HOTSEARCH_TEMPLATE` |

## 类定义与初始化（L33 ~ L98）

| 行号范围 | 内容 | 说明 |
|----------|------|------|
| L33 ~ L34 | `@register` 装饰器 + `class WeiboMonitor(Star)` | 插件注册，当前版本 v1.13.1 |
| L35 ~ L98 | `__init__` 方法 | 配置加载 → 数据目录创建 → 日志系统 → HTTP 客户端 → 并发信号量 → 旧路径数据迁移 → 持久化数据加载 → 热搜防刷屏初始化 → 启动后台监控任务 |

## 日志与时间工具（L100 ~ L178）

| 行号范围 | 内容 | 说明 |
|----------|------|------|
| L100 ~ L127 | `setup_logging()` | `RotatingFileHandler` 写入 `plugin.log`，控制台 `StreamHandler` 输出，避免重复添加 handler |
| L129 ~ L131 | `_get_utc8_now()` | 返回 UTC+8 带时区的 `datetime` 对象 |
| L133 ~ L178 | `_parse_weibo_time(time_str)` | 解析微博时间：「刚刚」「N 分钟前」「N 小时前」「昨天 HH:mm」「MM-DD」「YYYY-MM-DD」「Sat Mar 08 16:51:30 +0800 2025」 |

## 每日日志记录（L180 ~ L248）

| 行号范围 | 内容 | 说明 |
|----------|------|------|
| L180 ~ L226 | `_log_to_daily_file(post, skip_log)` | 记录微博动态到 `logs/YYYYMMDD.log`，JSON Lines 格式，按 link 去重 |
| L228 ~ L248 | `_log_hotsearch_to_daily(items)` | 记录热搜推送事件，`type: "hotsearch"` |

## 热搜防刷屏与每日总结（L250 ~ L360）

| 行号范围 | 内容 | 说明 |
|----------|------|------|
| L250 ~ L297 | `_init_last_hotsearch_time()` | 启动时扫描日志+持久化数据，30 分钟内已推送则回退 `last_hotsearch_time` |
| L299 ~ L360 | `_send_daily_summary()` | 读取昨日日志，统计推送次数，格式化推送到目标会话 |

## 热搜抓取与推送（L362 ~ L493）

| 行号范围 | 内容 | 说明 |
|----------|------|------|
| L362 ~ L450 | `_fetch_hotsearch()` | 先无 Cookie 请求 → 失败带 Cookie 兜底 → 429 限流处理 → 过滤广告 |
| L452 ~ L493 | `_push_hotsearch(items, targets)` | 格式化推送 + 记录日志 + 持久化 `last_hotsearch_push_time` |

## 数据持久化（L495 ~ L537）

| 行号范围 | 内容 | 说明 |
|----------|------|------|
| L495 ~ L509 | `_load_data()` | 加载 `monitor_data.json`，损坏时自动备份 |
| L511 ~ L529 | `_save_data()` | 原子写入（`.tmp` → replace） |
| L531 ~ L533 | `get_kv_data(key, default)` | 异步读取键值对 |
| L535 ~ L537 | `put_kv_data(key, value)` | 异步写入键值对并保存 |

## 请求头与生命周期（L540 ~ L582）

| 行号范围 | 内容 | 说明 |
|----------|------|------|
| L540 ~ L555 | `get_headers(uid)` | iPhone Safari UA、JSON Accept、Referer、Cookie |
| L557 ~ L566 | `terminate()` | 取消任务、关闭客户端 |
| L568 ~ L582 | `get_targets()` | 解析推送目标列表，兼容字符串/列表格式 |

## 用户命令区（L584 ~ L947）

| 行号范围 | 命令 | 说明 |
|----------|------|------|
| L584 ~ L589 | `/get_umo` | 获取当前会话 ID |
| L591 ~ L603 | `/weibo_export` | 导出配置为 Base64 |
| L605 ~ L646 | `/weibo_import <str>` | 导入配置（Base64/JSON） |
| L648 ~ L683 | `/weibo_verify` | 验证 Cookie 有效性 |
| L685 ~ L740 | `/weibo_cookie <cookie>` | 更换 Cookie + 重载插件 |
| L742 ~ L767 | `/weibo_check` | 立即检查第一个账号 |
| L769 ~ L805 | `/weibo_check_all` | 立即检查所有账号 |
| L807 ~ L828 | `/weibo_hot` | 手动查询热搜榜 |
| L830 ~ 871 | `/weibo_status` | 显示监控状态 |
| L873 ~ 947 | `/weibo_summary` | 手动触发昨日总结 |

## 属性与辅助方法（L947 ~ L967）

| 行号范围 | 内容 | 说明 |
|----------|------|------|
| L947 ~ L953 | `message_format` (property) | 消息模板，`\\n` → 真换行 |
| L955 ~ L967 | `_check_cookie_health()` | 异步检查 Cookie 有效性 |

## 后台监控主循环（L969 ~ L1111）

| 行号范围 | 内容 | 说明 |
|----------|------|------|
| L969 ~ L975 | 循环初始化 | 10 秒预热，初始化 `last_check_time` 和 `error_backoff` |
| L977 ~ L990 | 循环体入口 | 获取 UTC+8 时间、重置错误计数 |
| L991 ~ L1006 | 每日总结检查 | 判断 `daily_summary_time`，调用 `_send_daily_summary()` |
| L1008 ~ L1028 | 热搜监控检查 | 判断 `hotsearch_interval`，获取并推送热搜 |
| L1030 ~ L1102 | 微博监控检查 | Cookie 健康检查 → Cookie 失效通知 → `_process_monitor_cycle()` → 错误退避 |
| L1104 ~ L1111 | 异常处理 | `CancelledError` 正常退出、其他异常指数退避（最大 5 分钟） |

## 辅助解析方法（L1113 ~ L1147）

| 行号范围 | 内容 | 说明 |
|----------|------|------|
| L1113 ~ L1127 | `_parse_urls(urls_raw)` | 解析 URL 列表，兼容字符串/列表格式 |
| L1129 ~ L1147 | `_process_monitor_cycle(...)` | 单个监控周期：逐 URL 检查 |

## 消息推送（L1149 ~ L1183）

| 行号范围 | 内容 | 说明 |
|----------|------|------|
| L1149 ~ L1183 | `_send_new_posts(...)` | 格式化 → 记录日志 → 推送到目标会话 |

## 微博抓取核心链路（L1185 ~ L1468）

| 行号范围 | 内容 | 说明 |
|----------|------|------|
| L1185 ~ L1225 | `parse_uid(url)` | 纯数字 → `/u/数字` → `/n/用户名` 跳转（含 429 重试） |
| L1227 ~ L1251 | `_fetch_weibo_cards(uid)` | 请求 `m.weibo.cn` API，含 429 限流 |
| L1253 ~ L1278 | `_extract_valid_mblogs(cards)` | 提取有效博文，5 种置顶判断 |
| L1280 ~ L1328 | `check_weibo(uid, force_fetch)` | 核心：获取 → 提取 → last_id 比较 → 收集新帖 |
| L1330 ~ L1363 | `_initialize_monitor(uid, ...)` | 首次初始化：记录起始 ID，历史博文写入日志 |
| L1365 ~ L1424 | `_collect_new_posts(uid, ...)` | 过滤规则：last_id → 原创/转发 → 屏蔽词 → 白名单 |
| L1426 ~ L1432 | `_has_filter_keyword(text, ...)` | 黑名单匹配 |
| L1434 ~ L1443 | `_should_skip_by_whitelist(text, ...)` | 白名单匹配 |
| L1445 ~ L1452 | `_update_last_id(...)` | 更新最新微博 ID |
| L1454 ~ L1468 | `clean_text(text)` | HTML 清理：移除全文链接、alt 替换、链接保留文本、br 转换 |

## 快速定位指南

| 想要修改的功能 | 行号范围 |
|----------------|----------|
| 添加新命令 | L584 ~ L947 |
| 修改微博抓取逻辑 | L1185 ~ L1468 |
| 修改热搜抓取/推送 | L362 ~ L493 |
| 修改监控主循环 | L969 ~ L1111 |
| 修改数据持久化 | L495 ~ L537 |
| 修改消息过滤规则 | L1365 ~ L1443 |
| 修改每日日志格式 | L180 ~ L248 |
| 修改 Cookie 相关逻辑 | L540 ~ L555 + L648 ~ L740 + L955 ~ L967 |
| 修改初始化/启动逻辑 | L35 ~ L98 + L250 ~ L297 |
