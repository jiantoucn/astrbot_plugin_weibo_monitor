# 用户命令列表

| 命令 | 功能 | 关键实现 |
|------|------|----------|
| `/get_umo` | 获取当前会话 ID | `event.unified_msg_origin` |
| `/weibo_verify` | 验证 Cookie 有效性 | 请求 `m.weibo.cn/api/config`，检查 `login` 字段 |
| `/weibo_cookie <cookie>` | 更换 Cookie 并重载插件 | 更新 `self.config`，调用 `config_manager.save_config()`，尝试 `star_loader.reload()` |
| `/weibo_check` | 立即检查第一个账号 | `check_weibo(uid, force_fetch=True)` |
| `/weibo_check_all` | 立即检查所有账号 | 逐一调用 `check_weibo`，账号间有请求间隔 |
| `/weibo_hot` | 手动查询热搜 | `_fetch_hotsearch()` + `_push_hotsearch()` |
| `/weibo_status` | 查看监控状态 | 读取配置项并格式化输出 |
| `/weibo_summary` | 手动触发昨日总结 | 读取昨日日志文件，统计并推送 |
| `/weibo_export` | 导出配置 | JSON 序列化 + Base64 编码 |
| `/weibo_import <str>` | 导入配置 | 支持 Base64 或直接 JSON，键值合并更新 |
