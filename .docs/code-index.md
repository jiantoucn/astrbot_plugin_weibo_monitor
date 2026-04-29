# main.py 代码索引

> 轻量索引：按功能分区列出方法名，用 IDE 搜索方法名即可精确定位。
> 仅在**新增/删除整个方法**时需更新本文件，单行增删无需同步。

## 常量与类定义（文件头）

`DEFAULT_CHECK_INTERVAL`, `DEFAULT_REQUEST_INTERVAL`, `DEFAULT_TIMEOUT`, `MAX_CONCURRENT_REQUESTS`, `DEFAULT_MESSAGE_TEMPLATE`, `WEIBO_API_BASE`, `WEIBO_MOBILE_BASE`, `WEIBO_WEB_BASE`, `HOTSEARCH_API_URL`, `DEFAULT_HOTSEARCH_INTERVAL`, `DEFAULT_HOTSEARCH_TOP_N`, `DEFAULT_HOTSEARCH_TEMPLATE`

`class WeiboMonitor(Star)` — `__init__`

## 日志与时间工具

`setup_logging`, `_get_utc8_now`, `_parse_weibo_time`

## 每日日志记录

`_log_to_daily_file`, `_log_hotsearch_to_daily`

## 热搜防刷屏与每日总结

`_init_last_hotsearch_time`, `_send_daily_summary`

## 热搜抓取与推送

`_fetch_hotsearch`, `_push_hotsearch`

## 数据持久化

`_load_data`, `_save_data`, `get_kv_data`, `put_kv_data`

## 相似度去重

`_load_similarity_cache`, `_save_similarity_cache`, `_compute_simhash`, `_hamming_distance`, `_jaccard_similarity`, `_is_duplicate`, `_update_similarity_cache`

## 请求头与生命周期

`get_headers`, `terminate`, `get_targets`

## 用户命令

| 方法 | 命令 |
|------|------|
| `get_umo` | `/get_umo` |
| `weibo_export` | `/weibo_export` |
| `weibo_import` | `/weibo_import` |
| `weibo_verify` | `/weibo_verify` |
| `weibo_cookie` | `/weibo_cookie` |
| `weibo_check` | `/weibo_check` |
| `weibo_check_all` | `/weibo_check_all` |
| `weibo_hot` | `/weibo_hot` |
| `weibo_status` | `/weibo_status` |
| `weibo_summary` | `/weibo_summary` |

## 属性与辅助

`message_format` (property), `_check_cookie_health`

## 后台监控主循环

`run_monitor`

## 辅助解析

`_parse_urls`, `_process_monitor_cycle`

## 消息推送

`_send_new_posts`

## 微博抓取核心链路

`parse_uid`, `_fetch_weibo_cards`, `_extract_valid_mblogs`, `check_weibo`, `_initialize_monitor`, `_collect_new_posts`, `_has_filter_keyword`, `_should_skip_by_whitelist`, `_update_last_id`, `clean_text`
