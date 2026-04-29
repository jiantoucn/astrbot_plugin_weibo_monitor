# 项目结构与技术栈

## 项目文件

```
├── main.py                  # 唯一的 Python 源码文件，包含插件全部逻辑
├── _conf_schema.json        # 插件配置 Schema（WebUI 配置页面依据）
├── metadata.yaml            # 插件元数据（名称、版本、作者、仓库）
├── CHANGELOG.md             # 更新日志
├── README.md                # 用户文档
├── requirements.txt         # Python 依赖
├── logo.png                 # 插件图标
└── .gitignore
```

**重要**: 项目只有 `main.py` 一个 Python 源文件，所有逻辑（监控、推送、命令、配置、日志）都在其中。

## 技术栈

| 依赖 | 用途 |
|------|------|
| AstrBot 框架 | 插件宿主，提供 `Star`、`Context`、`filter`、`MessageChain` 等基础 API |
| httpx | 异步 HTTP 客户端，用于请求微博 API |
| BeautifulSoup4 | HTML 解析（部分场景备用） |

依赖声明在 `requirements.txt` 中：`httpx` 和 `beautifulsoup4`。
