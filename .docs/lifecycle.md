# 核心类 WeiboMonitor 生命周期

## 生命周期概览

1. `__init__`: 初始化配置、HTTP 客户端、数据目录、日志、持久化数据，启动后台监控任务
2. 后台监控任务 (`run_monitor`): 异步循环，定时检查微博和热搜
3. `terminate`: 停止监控任务，关闭 HTTP 客户端

## `__init__` 初始化顺序

```python
self.config = config or {}
self.data_dir = StarTools.get_data_dir()
self.data_file = self.data_dir / "monitor_data.json"
self.logs_dir = self.data_dir / "logs"
self.setup_logging()
self.client = httpx.AsyncClient(...)
self._data = self._load_data()
self._init_last_hotsearch_time()
self.monitor_task = asyncio.create_task(self.run_monitor())
```

## `run_monitor` 主循环

主循环每 60 秒迭代一次，依次检查：

1. **每日总结**: 判断是否到达设定的总结推送时间
2. **热搜监控**: 判断 `hotsearch_interval` 是否已过，到达后获取并推送热搜
3. **微博监控**: 判断 `check_interval`（含随机抖动）是否已过，到达后逐一检查监控列表

异常处理采用指数退避策略：连续错误时自动延长等待时间（最大 5 分钟）。

## 防重载刷屏机制

`_init_last_hotsearch_time()` 在插件初始化时执行：

1. 扫描今天和昨天的每日日志文件（`logs/YYYYMMDD.log`）
2. 检查 `self._data["last_hotsearch_push_time"]` 持久化记录
3. 若 30 分钟内有推送记录，计算 `elapsed` 并回退 `last_hotsearch_time`
4. 使 `run_monitor` 中的 interval 判断在等待足够时间后才触发首次推送
