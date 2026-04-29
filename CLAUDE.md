﻿# CLAUDE.md — AstrBot 微博监控插件项目指南

## 项目概述

AstrBot 微博监控插件，定时监控微博用户动态并推送到指定会话，同时支持微博热搜榜定时推送。
基于 AstrBot 插件框架开发，采用 Python 异步架构。

- **项目名称**: `astrbot_plugin_weibo_monitor`
- **技术栈**: Python 3.10+, AstrBot 框架, httpx, BeautifulSoup4
- **唯一源文件**: `main.py`（所有逻辑集中于此，约 1468 行）

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
