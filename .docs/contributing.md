# 贡献指南

## 贡献规则

1. **语言与沟通**
   - 代码注释和日志保持中文风格，与现有代码一致。

2. **版本号管理（SemVer）**
   - 每次提交更新或修改时，必须在回复显眼处给出当前版本号。
   - 若当前无版本号，默认从 `v1.0.0` 开始。
   - 版本号递增规则：
     - 修复/优化 → Patch（`x.y.Z`）
     - 新功能 → Minor（`x.Y.0`）
     - 仅在用户明确要求时 → Major（`X.0.0`）
   - 三处必须同步：`main.py @register`、`metadata.yaml`、`CHANGELOG.md`。

3. **文档同步更新**
   - 每次代码更新后，主动检查 `CHANGELOG.md` 与 `README.md` 是否需要同步修改。
   - 若文件存在且受影响，必须同步更新，确保文档与代码一致。

4. **代码行号索引维护**
   - `.docs/code-index.md` 中的 `main.py 代码行号索引` 会随代码变化而失效。
   - 在以下场景必须重新核对并更新行号索引：
     - 新增/删除/移动函数或命令
     - 重构代码块顺序
     - 调整初始化、监控循环、数据持久化等核心区域
   - 若改动较小，可仅更新受影响行号段；若结构变化较大，建议全量刷新索引。

5. **新增规则的规范化流程**
   - 新增任何开发规则时，应先写入本文件。
   - 若新规则影响代码位置映射，应立即更新 `.docs/code-index.md`。
   - 若新规则影响发布流程，应同时更新对应章节。

## 版本管理

- 版本号遵循 SemVer 语义化版本规范
- 版本号必须同步更新的位置:
  1. `main.py` 中 `@register` 装饰器的第 4 个参数
  2. `metadata.yaml` 中的 `version` 字段
  3. `CHANGELOG.md` 新增对应版本条目

## 开发注意事项

### 添加新命令

1. 在 `main.py` 中找到合适的插入位置（现有命令块之间）
2. 使用 `@filter.command("command_name")` 装饰器
3. 函数签名: `async def handler(self, event: AstrMessageEvent, arg: str = ""):`
4. 使用 `yield event.plain_result(text)` 返回响应
5. 更新 `README.md` 的常用指令列表

### 修改配置项

1. 在 `_conf_schema.json` 中添加/修改配置项定义（type, default, description, hint）
2. 在 `main.py` 中通过 `self.config.get("key", default)` 读取
3. 新功能的配置项同时更新 `README.md` 的配置说明

### 添加新 API 请求

- 使用 `self.client` (httpx.AsyncClient) 发起请求
- 使用 `self.get_headers()` 获取统一请求头
- 使用 `self._request_semaphore` 控制并发
- 处理 429 限流状态码
- 使用 `async with self._request_semaphore:` 包裹请求

### 测试注意事项

- Cookie 有效性直接影响大部分功能，测试时需确保 Cookie 可用
- 热搜功能默认无需 Cookie，但可能因风控需要 Cookie 兜底
- 检查间隔和请求间隔都有随机抖动机制，实际值在配置值 ± jitter 范围内
- 插件重载时有 30 分钟热搜推送防刷屏保护

### 代码风格

- 使用中文注释和日志信息
- 日志使用 `self.plugin_logger` 而非 `logging.getLogger()`
- 异常处理要全面，单个账号失败不应影响其他账号
- 配置读取使用 `self.config.get(key, default)` 提供默认值
- 不在代码中添加多余注释（除非逻辑复杂需要解释）
