# HTTP 客户端与请求

## 客户端配置

- **超时**: 20 秒 (`DEFAULT_TIMEOUT`)
- **自动重试**: 2 次 (`httpx.AsyncHTTPTransport(retries=2)`)
- **连接池**: 最大 5 并发，10 个保持连接
- **信号量**: `asyncio.Semaphore(5)` 限制并发请求
- **限流处理**: 遇到 429 状态码时等待 60 秒后重试
- **请求头**: 模拟 iPhone Safari 移动端 UA

## 时区处理

- 全程使用 UTC+8（北京时间）
- `_get_utc8_now()` 返回带时区信息的 `datetime` 对象
- `datetime.now(timezone(timedelta(hours=8)))`
- 持久化时间格式: `YYYY-MM-DD HH:MM:SS`（字符串，无时区后缀）
- 读取时需 `.replace(tzinfo=now.tzinfo)` 补回时区

## URL 解析规则

`parse_uid(url)` 支持的输入格式:
1. 纯数字 UID: `2803301701`
2. 移动端 URL: `https://m.weibo.cn/u/2803301701`
3. PC 端 URL: `https://weibo.com/u/2803301701`
4. 用户名 URL: `https://weibo.com/n/用户名`（需要网络请求解析跳转）
