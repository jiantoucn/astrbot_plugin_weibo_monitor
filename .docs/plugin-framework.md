# AstrBot 插件框架核心概念

## 插件注册

```python
@register("astrbot_plugin_weibo_monitor", "Sayaka", "描述", "版本号", "仓库地址")
class WeiboMonitor(Star):
```

- `@register` 装饰器将类注册为 AstrBot 插件
- 必须继承 `Star` 基类
- `__init__` 接收 `context: Context` 和 `config: dict` 两个参数

## 命令注册

```python
@filter.command("command_name")
async def handler(self, event: AstrMessageEvent, arg: str = ""):
    yield event.plain_result("响应内容")
```

- `@filter.command("name")` 注册聊天命令（用户发送 `/name` 触发）
- 命令处理函数必须是 `async` 生成器函数
- 通过 `yield event.plain_result(text)` 返回文本响应
- 通过 `await self.context.send_message(target, chain)` 主动推送消息
- 命令参数通过函数参数自动解析（字符串类型）

## 关键 API

- `self.context`: `Context` 对象，访问框架服务
- `self.context.send_message(target_id, message_chain)`: 向指定会话推送消息
- `event.unified_msg_origin`: 当前会话的唯一标识
- `MessageChain().message(text)`: 构建消息链
- `StarTools.get_data_dir()`: 获取插件数据存储目录
