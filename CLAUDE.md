# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

AstrBot 微博监控插件，定时监控微博用户动态并推送到指定会话，同时支持微博热搜榜定时推送。
基于 AstrBot 插件框架开发，采用 Python 异步架构。

- **项目名称**: `astrbot_plugin_weibo_monitor`
- **技术栈**: Python 3.10+, AstrBot 框架, httpx, BeautifulSoup4
- **唯一源文件**: `main.py`（所有逻辑集中于此，约 1797 行）
- **无测试套件、无 linter 配置** — 依赖手动测试和 AstrBot 插件加载验证

## 架构要点

整个插件是单个类 `WeiboMonitor(Star)`，通过 `@register` 装饰器注册到 AstrBot 框架。

### 核心数据流

```
微博监控:  parse_uid → _fetch_weibo_cards → _extract_valid_mblogs → check_weibo
           → _collect_new_posts → _get_targets_for_uid → _send_new_posts

热搜推送:  _fetch_hotsearch → _push_hotsearch

每日总结:  _send_daily_summary（读取 logs/YYYYMMDD.log）
```

### 后台主循环 (`run_monitor`)

每 60 秒迭代，依次检查：每日总结 → 热搜推送 → 微博监控周期。连续错误时指数退避（最大 5 分钟）。

### 命令注册模式

```python
@filter.command("command_name")
async def handler(self, event: AstrMessageEvent, arg: str = ""):
    yield event.plain_result("响应内容")
```

### 关键设计

- **并发控制**: `asyncio.Semaphore(5)` 限制并发 HTTP 请求
- **随机抖动**: 检查间隔和请求间隔均有 ±jitter 防反爬
- **原子写入**: `_save_data` 使用 temp file + replace 模式
- **订阅分组**: `_get_targets_for_uid` 按 UID 路由到不同会话
- **Cookie 兜底**: 持久化数据中备份 Cookie，框架配置丢失时自动恢复

## 版本管理（SemVer）

每次修改必须同步更新三处：
1. `main.py` 中 `@register` 装饰器的第 4 个参数
2. `metadata.yaml` 中的 `version` 字段
3. `CHANGELOG.md` 新增对应版本条目

递增规则：修复/优化 → Patch（`x.y.Z`），新功能 → Minor（`x.Y.0`），仅用户明确要求 → Major（`X.0.0`）。

## 代码风格

- 中文注释和日志信息
- 日志使用 `self.plugin_logger` 而非 `logging.getLogger()`
- 配置读取使用 `self.config.get(key, default)` 提供默认值
- 异常处理要全面，单个账号失败不应影响其他账号
- `_conf_schema.json` 中配置项 description 必须以 `【全局】` 或 `【分组】` 前缀开头

## 详细文档索引

> 以下文档按主题拆分于 `.docs/` 目录，按需阅读，避免无关内容污染上下文窗口。

### 项目结构

- `.docs/project-structure.md` — 文件结构与技术栈
- `.docs/code-index.md` — `main.py` 全量代码行号索引（修改代码后必须同步更新）
- `.docs/data-flows.md` — 微博抓取、热搜抓取、每日总结的数据流图

### 框架与运行机制

- `.docs/plugin-framework.md` — AstrBot 插件注册、命令注册、关键 API
- `.docs/commands.md` — 完整用户命令列表
- `.docs/lifecycle.md` — WeiboMonitor 生命周期、初始化顺序、主循环、防刷屏机制

### 配置与数据

- `.docs/config-params.md` — 所有配置项分组说明
- `.docs/persistence.md` — 持久化数据文件与每日日志格式
- `.docs/http-client.md` — HTTP 客户端配置、时区处理、URL 解析规则

### 开发规范

- `.docs/contributing.md` — 贡献规则、版本管理、开发注意事项、代码风格

## 快速触发规则

- 修改 `main.py` 代码行号后，请更新 `.docs/code-index.md`。
- 涉及版本号变更时，必须同步更新 `main.py @register`、`metadata.yaml`、`CHANGELOG.md`，详见 `.docs/contributing.md`。
- 涉及新增命令或修改配置时，请参考 `.docs/contributing.md` 开发注意事项。
- 涉及 AstrBot 框架 API 使用时，请参考 `.docs/plugin-framework.md`。
- 涉及配置项时，请参考 `.docs/config-params.md`；涉及数据持久化时，请参考 `.docs/persistence.md`。
