# 数据持久化

## 持久化数据文件

路径: `StarTools.get_data_dir() / "monitor_data.json"`

存储内容:
- 各监控账号的最后推送微博 ID (`last_id_{uid}`)
- `last_summary_date`: 上次推送每日总结的日期
- `last_hotsearch_push_time`: 最近一次热搜推送时间（UTC+8 格式 `YYYY-MM-DD HH:MM:SS`）

读写方法:
- `_load_data()`: 加载数据，损坏时自动备份
- `_save_data()`: 原子写入（先写 `.tmp` 再替换），防止数据损坏

## 每日日志文件

目录: `StarTools.get_data_dir() / "logs"`
文件名格式: `YYYYMMDD.log`
格式: 每行一个 JSON 对象

微博动态记录:
```json
{"type": "weibo", "username": "用户名", "text": "正文", "link": "链接", "time": "2026-01-01 12:00:00"}
```

热搜推送记录:
```json
{"type": "hotsearch", "time": "2026-01-01 12:00:00", "count": 10, "items": ["热搜1", "热搜2"]}
```
