# 配置项说明

## 配置文件

- `_conf_schema.json`: 定义所有配置项的类型、默认值、描述、提示信息
- 配置通过 AstrBot WebUI 的插件设置页面管理
- 运行时通过 `self.config` 字典访问

## 基础配置

- `weibo_cookie` (string): 微博 Cookie，必填
- `weibo_urls` (list): 监控的微博用户 URL/UID 列表
- `target_conversation_id` (list): 推送目标会话 ID 列表

## 监控控制

- `check_interval` (int, 默认 10): 检查间隔（分钟）
- `check_interval_jitter` (int, 默认 2): 检查间隔随机浮动（分钟）
- `request_interval` (int, 默认 5): 账号间请求间隔（秒）
- `request_interval_jitter` (int, 默认 1): 请求间隔随机浮动（秒）

## 过滤规则

- `filter_keywords` (list): 屏蔽词，包含这些词的微博不推送
- `whitelist_keywords` (list): 白名单，只推送包含这些词的微博
- `send_original` (bool, 默认 true): 推送原创微博
- `send_forward` (bool, 默认 true): 推送转发微博

## 消息格式

- `message_format` (string): 微博推送模板，变量: `{name}`, `{weibo}`, `{link}`
- `hotsearch_message_format` (string): 热搜推送模板，变量: `{top_n}`, `{time}`, `{items}`

## 热搜配置

- `enable_hotsearch` (bool, 默认 false): 开启热搜监控
- `hotsearch_interval` (int, 默认 60): 热搜推送间隔（分钟）
- `hotsearch_top_n` (int, 默认 10): 推送热搜前 N 条
- `hotsearch_filter_ads` (bool, 默认 true): 过滤热搜广告

## 日志配置

- `enable_plugin_log` (bool): 开启运行日志 plugin.log
- `enable_daily_log` (bool): 开启每日推送记录
- `enable_daily_summary` (bool): 开启每日总结
- `daily_summary_time` (string, 默认 "08:00"): 总结推送时间
