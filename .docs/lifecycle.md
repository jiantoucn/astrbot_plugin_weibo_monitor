# WeiboMonitor 生命周期

## 初始化顺序 (`__init__`)

1. 加载配置 → 创建数据目录 (`data_dir`, `logs_dir`)
2. 初始化日志 (`setup_logging`) → 检查 Cookie 配置
3. 创建 httpx 客户端（20s 超时、2 次重试、5 并发信号量）
4. 兼容旧路径迁移 → 加载持久化数据 (`monitor_data.json`)
5. 初始化热搜防刷屏时间 → 加载相似度缓存 (`similarity_cache.json`)
6. 启动后台监控任务 (`run_monitor`)

## 主循环 (`run_monitor`)

每 60 秒迭代一次，依次检查：

1. **每日总结**: 是否到达 `daily_summary_time`
2. **热搜监控**: 是否超过 `hotsearch_interval`
3. **微博监控**: 是否超过 `check_interval`（含随机抖动）→ 逐账号检查 → 推送

错误处理：连续错误时指数退避（最大 5 分钟），正常运行后自动恢复。

## 终止 (`terminate`)

取消后台任务 → 关闭 httpx 客户端。

## 防重载刷屏

`_init_last_hotsearch_time()` 扫描日志和持久化数据，若 30 分钟内已推送热搜则回退计时器，避免重载后立即刷屏。
