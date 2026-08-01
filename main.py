import asyncio
import re
import httpx
import os
import json
import base64
import inspect
import random
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple, Dict, Any
from urllib.parse import quote
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api.web import error_response, json_response, request
from bs4 import BeautifulSoup
import astrbot.api.message_components as Comp

# 常量定义
DEFAULT_CHECK_INTERVAL = 10  # 默认检查间隔（分钟）
DEFAULT_REQUEST_INTERVAL = 5  # 默认请求间隔（秒）
DEFAULT_TIMEOUT = 20  # 默认HTTP请求超时（秒）
MAX_CONCURRENT_REQUESTS = 5  # 最大并发请求数
MAX_PUSH_QUEUE_SIZE = 100  # 推送队列最大积压条目数
DEFAULT_MESSAGE_TEMPLATE = "🔔 {name} 发微博啦！\n\n{weibo}\n\n链接: {link}"
WEIBO_API_BASE = "https://m.weibo.cn/api/container/getIndex"
WEIBO_MOBILE_BASE = "https://m.weibo.cn"
WEIBO_WEB_BASE = "https://weibo.com"
HOTSEARCH_API_URL = "https://weibo.com/ajax/side/hotSearch"
DEFAULT_HOTSEARCH_INTERVAL = 60
DEFAULT_HOTSEARCH_TOP_N = 10
DEFAULT_HOTSEARCH_TEMPLATE = "🔥 微博热搜榜 Top {top_n}\n⏰ 更新时间: {time}\n\n{items}"
PLUGIN_NAME = "astrbot_plugin_weibo_monitor"

CONFIG_GROUPS = {
    "account_settings": ("weibo_urls", "weibo_cookie", "cookie_notification_target"),
    "schedule_settings": (
        "check_interval",
        "check_interval_jitter",
        "request_interval",
        "request_interval_jitter",
    ),
    "content_settings": ("message_format", "send_original", "send_forward"),
    "media_settings": (
        "enable_image_download",
        "max_images_per_post",
        "enable_video_download",
        "max_video_size_mb",
        "video_download_timeout",
        "video_send_timeout",
        "temp_media_retention_minutes",
    ),
    "filter_settings": ("filter_keywords", "whitelist_keywords"),
    "logging_settings": (
        "enable_plugin_log",
        "plugin_log_max_size",
        "enable_daily_log",
        "enable_daily_summary",
        "daily_summary_time",
    ),
    "hotsearch_settings": (
        "enable_hotsearch",
        "hotsearch_interval",
        "hotsearch_top_n",
        "hotsearch_filter_ads",
        "hotsearch_show_link",
        "hotsearch_message_format",
    ),
}
CONFIG_KEY_GROUPS = {
    key: group for group, keys in CONFIG_GROUPS.items() for key in keys
}


@register(
    "astrbot_plugin_weibo_monitor",
    "Sayaka",
    "定时监控微博用户动态并推送到指定会话，支持按会话分组订阅不同博主。",
    "v1.19.6",
    "https://github.com/jiantoucn/astrbot_plugin_weibo_monitor",
)
class WeiboMonitor(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.monitor_task: Optional[asyncio.Task] = None
        self.push_consumer_task: Optional[asyncio.Task] = None
        self._migrate_persist_task: Optional[asyncio.Task] = None
        self.cookie_invalid_notified = False  # cookie 失效是否已通知
        self.push_queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_PUSH_QUEUE_SIZE)

        # 确保数据目录存在
        self.data_dir = StarTools.get_data_dir()
        if not self.data_dir.exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_file = self.data_dir / "monitor_data.json"
        self.logs_dir = self.data_dir / "logs"
        if not self.logs_dir.exists():
            self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.temp_images_dir = self.data_dir / "temp_images"
        if not self.temp_images_dir.exists():
            self.temp_images_dir.mkdir(parents=True, exist_ok=True)

        # 初始化日志
        self.plugin_logger = logging.getLogger("astrbot_plugin_weibo_monitor")
        self.plugin_logger.setLevel(logging.DEBUG)
        self.plugin_logger.propagate = False  # 不向上冒泡到 root logger
        self.setup_logging()
        self._migrate_grouped_config()
        self._register_web_apis()

        # 配置HTTP客户端，添加重试、超时和连接池设置
        self.limits = httpx.Limits(
            max_keepalive_connections=10,
            max_connections=MAX_CONCURRENT_REQUESTS,
            keepalive_expiry=30.0,
        )
        transport = httpx.AsyncHTTPTransport(retries=2)
        self.client = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            transport=transport,
            follow_redirects=True,
            limits=self.limits,
        )
        self.running = True
        self.session_initialized_uids: set[str] = set()
        self.last_summary_date: str = ""
        self._request_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        self._consecutive_errors = 0
        self._max_error_backoff = 300  # 最大退避时间5分钟

        # 兼容旧路径迁移 (data/astrbot_plugin_weibo_monitor -> StarTools.get_data_dir())
        old_data_file = os.path.join(
            "data", "astrbot_plugin_weibo_monitor", "monitor_data.json"
        )
        if not self.data_file.exists() and os.path.exists(old_data_file):
            try:
                import shutil

                shutil.copy2(old_data_file, self.data_file)
                self.plugin_logger.info(
                    f"WeiboMonitor: 已从旧路径迁移数据到 {self.data_file}"
                )
            except Exception as e:
                self.plugin_logger.error(f"WeiboMonitor: 迁移数据失败: {e}")

        self._data = self._load_data()

        # 订阅分组由 Plugin Page 管理。AstrBot 升级时若框架配置被默认值覆盖，
        # 从持久化快照恢复，避免用户重新配置全部会话和博主。
        self._restore_subscription_backup()
        self._ensure_subscription_backup()

        # 迁移旧版配置：将 target_conversation_id 合并到 subscription_mappings（同步，确保在 run_monitor 前完成）
        self._migrate_config_v2()

        # 检查Cookie是否配置，若框架配置为空则尝试从 _data 兜底恢复
        if not self._get_config("weibo_cookie", ""):
            backup_cookie = self._data.get("_backup_weibo_cookie", "")
            if backup_cookie:
                self._set_config("weibo_cookie", backup_cookie)
                self.plugin_logger.info("WeiboMonitor: 从持久化数据中恢复了微博 Cookie")
            else:
                self.plugin_logger.warning(
                    "WeiboMonitor: 未配置微博Cookie，插件无法正常工作！请在插件设置中填写weibo_cookie。"
                )

        # 页面状态只读展示：不参与监控决策，避免状态接口异常影响主循环。
        configured_cookie = bool(self._get_config("weibo_cookie", ""))
        self.cookie_health_status = (
            self._data.get("_cookie_health_status", "unknown")
            if configured_cookie
            else "unconfigured"
        )
        self.cookie_health_checked_at = self._data.get(
            "_cookie_health_checked_at", ""
        )
        self.next_push_time = ""

        self.last_summary_date = self._data.get("last_summary_date", "")
        self.last_hotsearch_time = 0
        self._init_last_hotsearch_time()

        # 启动后台监控任务
        self.monitor_task = asyncio.create_task(self.run_monitor())
        self.push_consumer_task = asyncio.create_task(self._push_consumer())

    def setup_logging(self):
        """设置运行日志"""
        existing_handlers = self.plugin_logger.handlers
        if any(isinstance(h, logging.FileHandler) for h in existing_handlers):
            return

        for handler in existing_handlers:
            if not isinstance(handler, logging.FileHandler):
                self.plugin_logger.removeHandler(handler)

        if self._get_config("enable_plugin_log", False):
            log_file = self.data_dir / "plugin.log"
            max_size_mb = self._get_config("plugin_log_max_size", 1)
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=max_size_mb * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            formatter = logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            )
            file_handler.setFormatter(formatter)
            self.plugin_logger.addHandler(file_handler)
            self.plugin_logger.info("运行日志功能已启用")

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        if not any(
            isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
            for h in self.plugin_logger.handlers
        ):
            self.plugin_logger.addHandler(console_handler)

    def _get_config(self, key: str, default=None):
        """读取分组后的配置，同时兼容尚未迁移的旧版扁平配置。"""
        group = CONFIG_KEY_GROUPS.get(key)
        if group:
            group_config = self.config.get(group)
            if isinstance(group_config, dict) and key in group_config:
                return group_config[key]
        return self.config.get(key, default)

    def _set_config(self, key: str, value: Any):
        """写入分组配置；未分组字段继续写入顶层。"""
        group = CONFIG_KEY_GROUPS.get(key)
        if group:
            group_config = self.config.get(group)
            if not isinstance(group_config, dict):
                group_config = {}
                self.config[group] = group_config
            group_config[key] = value
            return
        self.config[key] = value

    def _migrate_grouped_config(self):
        """将 v1.18.x 的扁平配置一次性复制到分组配置中。"""
        if self.config.get("_config_schema_version", 0) >= 1:
            return

        changed = False
        for group, keys in CONFIG_GROUPS.items():
            group_config = self.config.get(group)
            if not isinstance(group_config, dict):
                group_config = {}
                self.config[group] = group_config
                changed = True
            for key in keys:
                if key in self.config:
                    group_config[key] = self.config[key]
                    changed = True

        self.config["_config_schema_version"] = 1
        changed = True
        if changed:
            self._save_plugin_config("分组配置迁移")

    async def _save_plugin_config_async(self, reason: str = "配置") -> bool:
        """等待 AstrBot 配置真正持久化完成，供页面保存接口确认结果。"""
        try:
            save_async = getattr(self.config, "save_config_async", None)
            if callable(save_async):
                result = save_async()
                if inspect.isawaitable(result):
                    await result
                return True

            save_sync = getattr(self.config, "save_config", None)
            if callable(save_sync):
                result = save_sync()
                if inspect.isawaitable(result):
                    await result
                return True

            if hasattr(self.context, "config_manager") and hasattr(
                self.context.config_manager, "save_config"
            ):
                result = self.context.config_manager.save_config()
                if inspect.isawaitable(result):
                    await result
                return True
        except Exception as e:
            self.plugin_logger.warning(f"{reason}保存失败: {e}")
            return False

        self.plugin_logger.warning(
            f"{reason}保存失败：AstrBot 未提供可用的配置持久化接口"
        )
        return False

    def _save_plugin_config(self, reason: str = "配置"):
        """后台保存配置，供初始化迁移等不需要阻塞的场景使用。"""
        try:
            asyncio.get_running_loop().create_task(
                self._save_plugin_config_async(reason)
            )
        except RuntimeError:
            # 插件初始化理论上运行在事件循环中；若框架在循环外调用，至少保留同步兜底。
            save_sync = getattr(self.config, "save_config", None)
            try:
                if callable(save_sync):
                    save_sync()
                elif hasattr(self.context, "config_manager") and hasattr(
                    self.context.config_manager, "save_config"
                ):
                    self.context.config_manager.save_config()
            except Exception as e:
                self.plugin_logger.warning(f"{reason}保存失败（不影响运行）: {e}")

    def _register_web_apis(self):
        """注册订阅分组页面使用的后端接口。"""
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/subscription-mappings",
            self.get_subscription_mappings,
            ["GET"],
            "获取微博订阅分组",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/subscription-mappings",
            self.save_subscription_mappings,
            ["POST"],
            "保存微博订阅分组",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/push-statistics",
            self.get_push_statistics,
            ["GET"],
            "获取微博推送统计",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/config-export",
            self.get_config_export,
            ["GET"],
            "导出微博监控配置命令",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/runtime-status",
            self.get_runtime_status,
            ["GET"],
            "获取微博监控运行状态",
        )

    def _build_export_command(self) -> str:
        """生成与 /weibo_export 相同格式的导入命令。"""
        legacy_keys = set(CONFIG_KEY_GROUPS) | {
            "_config_schema_version",
            "target_conversation_id",
        }
        export_config = {
            key: value
            for key, value in self.config.items()
            if key not in legacy_keys
        }
        config_json = json.dumps(export_config, ensure_ascii=False)
        config_b64 = base64.b64encode(config_json.encode("utf-8")).decode("utf-8")
        return f"/weibo_import {config_b64}"

    async def get_config_export(self):
        """供 Plugin Page 获取配置导入命令；不写日志，避免 Cookie 泄露。"""
        try:
            return json_response({"command": self._build_export_command()})
        except Exception as error:
            self.plugin_logger.error(f"WeiboMonitor: 页面导出配置失败: {error}")
            return error_response("导出配置失败，请查看插件日志", status_code=500)

    async def get_runtime_status(self):
        """供 Plugin Page 定时刷新运行状态。"""
        return json_response(self._get_runtime_status_for_page())

    @staticmethod
    def _is_complete_subscription_reference(item: str) -> bool:
        """判断条目是否完整的 UID 或 /u/ 主页链接，避免会话 ID 的片段被误识别。"""
        item = item.strip()
        return (
            item.isdigit()
            or re.fullmatch(
                r"https?://(?:m\.)?weibo\.(?:com|cn)/u/\d+/?(?:[?#].*)?", item
            )
            is not None
        )

    def _split_subscription_mapping(
        self, raw_mapping: Any
    ) -> Optional[Tuple[str, str]]:
        """拆分“会话 ID: UID 列表”，允许会话 ID 与微博 URL 中包含冒号。"""
        raw = str(raw_mapping).strip()
        for separator_index in reversed(
            [match.start() for match in re.finditer(":", raw)]
        ):
            session_id = raw[:separator_index].strip()
            raw_uids = raw[separator_index + 1 :].strip()
            if not session_id:
                continue
            if not raw_uids or raw_uids == "*":
                return session_id, raw_uids
            uids = [item.strip() for item in raw_uids.split(",") if item.strip()]
            if uids and all(
                self._is_complete_subscription_reference(uid) for uid in uids
            ):
                return session_id, raw_uids
        return None

    def _parse_mapping_for_page(self, raw_mapping: Any) -> Dict[str, Any]:
        """解析一条旧格式映射，保留异常原文供页面提示修复。"""
        raw = str(raw_mapping).strip()
        if not raw:
            return {"raw": raw, "valid": False, "error": "空白配置行"}
        if ":" not in raw:
            return {
                "raw": raw,
                "valid": False,
                "error": "缺少冒号，请使用“会话 ID: *”或“会话 ID: UID”格式",
            }

        parsed = self._split_subscription_mapping(raw)
        if not parsed:
            return {
                "raw": raw,
                "valid": False,
                "error": "未找到有效的 UID 列表；会话 ID 可包含冒号，请在最后一个会话 ID 后填写 : UID 或 : *",
            }
        session_id, raw_uids = parsed
        delivery = self._get_delivery_options(session_id, raw_uids == "*")
        if not raw_uids or raw_uids == "*":
            return {
                "raw": raw,
                "valid": True,
                "session_id": session_id,
                "mode": "all",
                "uids": [],
                **delivery,
            }

        uids = [item.strip() for item in raw_uids.split(",") if item.strip()]
        if not uids:
            return {"raw": raw, "valid": False, "error": "指定模式至少需要一个微博 UID"}
        invalid_uids = [
            item for item in uids if not self._resolve_uid_from_config(item)
        ]
        if invalid_uids:
            return {
                "raw": raw,
                "valid": False,
                "error": f"包含无效 UID 或微博链接：{', '.join(invalid_uids)}",
            }
        return {
            "raw": raw,
            "valid": True,
            "session_id": session_id,
            "mode": "uids",
            "uids": uids,
            **delivery,
        }

    def _get_delivery_options(
        self, session_id: str, legacy_all: bool = False
    ) -> Dict[str, bool]:
        """读取会话的热搜和总结接收设置；旧版 * 配置保持原有全选行为。"""
        options = self.config.get("subscription_delivery_options", {})
        session_options = (
            options.get(session_id, {}) if isinstance(options, dict) else {}
        )
        if not isinstance(session_options, dict):
            session_options = {}
        return {
            "receive_hotsearch": session_options.get("receive_hotsearch", legacy_all)
            is True,
            "receive_daily_summary": session_options.get(
                "receive_daily_summary", legacy_all
            )
            is True,
        }

    def _get_account_profiles(self) -> Dict[str, Dict[str, Any]]:
        """读取博主资料缓存，自动忽略旧版本或损坏的数据。"""
        profiles = self._data.get("account_profiles", {})
        if not isinstance(profiles, dict):
            return {}
        return {
            str(uid): profile
            for uid, profile in profiles.items()
            if isinstance(profile, dict)
        }

    @staticmethod
    def _safe_profile_url(value: Any) -> str:
        """只接受微博资料中的 HTTP(S) 图片地址。"""
        url = str(value or "").strip()
        return url if re.match(r"^https?://", url, re.IGNORECASE) else ""

    @staticmethod
    def _optional_profile_bool(value: Any, fallback: Any = None) -> Optional[bool]:
        """兼容微博接口可能返回的 bool 或 0/1，同时保留未知状态。"""
        if isinstance(value, bool):
            return value
        if value in (0, 1):
            return bool(value)
        return fallback if isinstance(fallback, bool) else None

    def _update_account_profile(self, uid: str, user: Any):
        """从已有微博响应被动更新博主资料；失败不影响抓取和推送。"""
        if not isinstance(user, dict):
            return

        profile_uid = str(user.get("idstr") or user.get("id") or uid).strip()
        if not profile_uid or profile_uid != str(uid):
            return

        profiles = self._get_account_profiles()
        previous = profiles.get(profile_uid, {})
        screen_name = str(user.get("screen_name") or "").strip()
        avatar_url = self._safe_profile_url(
            user.get("avatar_hd")
            or user.get("avatar_large")
            or user.get("profile_image_url")
        )
        verified = self._optional_profile_bool(
            user.get("verified"), previous.get("verified")
        )
        profile = {
            "uid": profile_uid,
            "screen_name": screen_name or str(previous.get("screen_name") or ""),
            "avatar_url": avatar_url
            or self._safe_profile_url(previous.get("avatar_url")),
            "verified": verified is True,
            "verified_type": user.get(
                "verified_type", previous.get("verified_type")
            ),
            "verified_reason": str(
                user.get("verified_reason", previous.get("verified_reason")) or ""
            ).strip(),
            "following": self._optional_profile_bool(
                user.get("following"), previous.get("following")
            ),
            "follow_me": self._optional_profile_bool(
                user.get("follow_me"), previous.get("follow_me")
            ),
        }
        comparable_previous = {
            key: previous.get(key) for key in profile if key != "uid"
        }
        comparable_profile = {
            key: value for key, value in profile.items() if key != "uid"
        }
        if comparable_previous == comparable_profile:
            return

        profile["updated_at"] = self._get_utc8_now().strftime("%Y-%m-%d %H:%M:%S")
        profiles[profile_uid] = profile
        self._data["account_profiles"] = profiles
        if not self._save_data():
            self.plugin_logger.warning(
                f"WeiboMonitor: UID {profile_uid} 的博主资料缓存保存失败"
            )

    def _get_monitored_account_options(self) -> List[Dict[str, Any]]:
        """返回带资料缓存的监控 UID，供页面展示头像、昵称和关注状态。"""
        options = []
        seen_uids = set()
        profiles = self._get_account_profiles()
        for raw_url in self._parse_urls(self._get_config("weibo_urls", [])):
            uid = self._resolve_uid_from_config(raw_url)
            if uid and uid not in seen_uids:
                seen_uids.add(uid)
                profile = profiles.get(uid, {})
                screen_name = str(profile.get("screen_name") or "").strip()
                options.append(
                    {
                        "uid": uid,
                        "label": f"{screen_name} · UID {uid}"
                        if screen_name
                        else f"UID {uid}",
                        "screen_name": screen_name,
                        "avatar_url": self._safe_profile_url(
                            profile.get("avatar_url")
                        ),
                        "verified": profile.get("verified") is True,
                        "verified_reason": str(
                            profile.get("verified_reason") or ""
                        ).strip(),
                        "following": profile.get("following")
                        if isinstance(profile.get("following"), bool)
                        else None,
                        "follow_me": profile.get("follow_me")
                        if isinstance(profile.get("follow_me"), bool)
                        else None,
                        "updated_at": str(profile.get("updated_at") or ""),
                    }
                )
        return options

    def _normalize_monitored_urls(self, raw_values: Any) -> List[str]:
        """校验页面提交的监控博主，兼容用户名主页链接。"""
        if not isinstance(raw_values, list):
            raise ValueError("monitor_urls 必须是数组")

        normalized = []
        seen = set()
        for raw_value in raw_values:
            raw = str(raw_value).strip()
            if not raw:
                continue
            uid = self._resolve_uid_from_config(raw)
            if uid:
                value = uid
            elif re.fullmatch(r"https?://(?:m\.)?weibo\.(?:com|cn)/n/[^/?#]+/?", raw):
                value = raw.rstrip("/")
            else:
                raise ValueError(f"不是有效的微博 UID 或主页链接：{raw}")
            if value not in seen:
                seen.add(value)
                normalized.append(value)
        return normalized

    def _save_subscription_backup(
        self,
        mappings: List[str],
        delivery_options: Dict[str, Dict[str, bool]],
        monitor_urls: List[str],
    ) -> bool:
        """将订阅页面配置写入独立快照，防止插件更新重置框架配置。"""
        self._data["_subscription_page_backup"] = {
            "subscription_mappings": list(mappings),
            "subscription_delivery_options": delivery_options,
            "weibo_urls": list(monitor_urls),
            "saved_at": self._get_utc8_now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        return self._save_data()

    def _has_valid_subscription_mappings(self, mappings: Any) -> bool:
        """判断订阅映射中是否至少存在一条页面格式可识别的有效配置。"""
        if isinstance(mappings, str):
            mappings = [item.strip() for item in mappings.splitlines() if item.strip()]
        if not isinstance(mappings, list):
            return False
        return any(
            self._parse_mapping_for_page(item).get("valid", False) for item in mappings
        )

    def _restore_subscription_backup(self):
        """框架配置为空或全部无效时恢复订阅快照，保留部分有效配置供用户修复。"""
        backup = self._data.get("_subscription_page_backup")
        if not isinstance(backup, dict):
            return

        backup_mappings = backup.get("subscription_mappings")
        backup_options = backup.get("subscription_delivery_options")
        backup_urls = backup.get("weibo_urls")
        if (
            not isinstance(backup_mappings, list)
            or not isinstance(backup_options, dict)
            or not isinstance(backup_urls, list)
        ):
            self.plugin_logger.warning("订阅分组备份格式无效，已跳过恢复")
            return

        restored = []
        current_mappings = self.config.get("subscription_mappings", [])
        backup_mappings_valid = self._has_valid_subscription_mappings(backup_mappings)
        current_mappings_valid = self._has_valid_subscription_mappings(current_mappings)
        if backup_mappings and backup_mappings_valid and not current_mappings_valid:
            self.config["subscription_mappings"] = list(backup_mappings)
            restored.append("订阅分组")

        current_options = self.config.get("subscription_delivery_options", {})
        if not current_options and backup_options:
            self.config["subscription_delivery_options"] = backup_options
            restored.append("附加推送选项")

        current_urls = self._parse_urls(self._get_config("weibo_urls", []))
        if not current_urls and backup_urls:
            self._set_config("weibo_urls", list(backup_urls))
            restored.append("监控博主")

        if restored:
            self.plugin_logger.warning(
                f"检测到框架订阅配置为空或全部无效，已从备份恢复：{'、'.join(restored)}"
            )
            self._save_plugin_config("订阅分组自动恢复")

    def _ensure_subscription_backup(self):
        """首次升级到带备份版本时，为已有页面配置补建恢复快照。"""
        existing_backup = self._data.get("_subscription_page_backup")
        if isinstance(existing_backup, dict):
            return

        mappings = self.config.get("subscription_mappings", [])
        if isinstance(mappings, str):
            mappings = [item.strip() for item in mappings.splitlines() if item.strip()]
        if not isinstance(mappings, list):
            mappings = []
        options = self.config.get("subscription_delivery_options", {})
        if not isinstance(options, dict):
            options = {}
        monitor_urls = self._parse_urls(self._get_config("weibo_urls", []))
        if mappings or options or monitor_urls:
            if self._save_subscription_backup(mappings, options, monitor_urls):
                self.plugin_logger.info("已为现有订阅分组创建升级保护备份")
            else:
                self.plugin_logger.warning(
                    "现有订阅分组备份创建失败，请检查插件数据目录权限"
                )

    async def get_subscription_mappings(self):
        """供 Plugin Page 读取结构化订阅分组。"""
        raw_mappings = self.config.get("subscription_mappings", [])
        if isinstance(raw_mappings, str):
            raw_mappings = raw_mappings.splitlines()
        if not isinstance(raw_mappings, list):
            raw_mappings = [raw_mappings]

        parsed = [self._parse_mapping_for_page(raw) for raw in raw_mappings]
        return json_response(
            {
                "rows": [item for item in parsed if item["valid"]],
                "invalid_rows": [item for item in parsed if not item["valid"]],
                "monitored_accounts": self._get_monitored_account_options(),
                "monitor_urls": self._parse_urls(self._get_config("weibo_urls", [])),
                "runtime_status": self._get_runtime_status_for_page(),
            }
        )

    @staticmethod
    def _display_status_time(value: Any) -> str:
        """将持久化时间转换为页面显示的 HH:MM:SS，避免暴露多余信息。"""
        if not value:
            return ""
        text = str(value).strip()
        try:
            return datetime.fromisoformat(text).strftime("%H:%M:%S")
        except ValueError:
            match = re.search(r"(\d{2}:\d{2}:\d{2})", text)
            return match.group(1) if match else ""

    def _get_runtime_status_for_page(self) -> Dict[str, str]:
        """返回页面所需的最小运行状态，不执行网络请求。"""
        cookie_configured = bool(self._get_config("weibo_cookie", ""))
        cookie_status = (
            self.cookie_health_status if cookie_configured else "unconfigured"
        )
        if cookie_status not in {"valid", "invalid", "unknown", "unconfigured"}:
            cookie_status = "unknown"
        return {
            "last_push_time": self._display_status_time(
                self._data.get("last_push_time", "")
            ),
            "next_push_time": self._display_status_time(self.next_push_time),
            "cookie_status": cookie_status,
            "cookie_checked_at": self._display_status_time(
                self.cookie_health_checked_at
            ),
        }

    def _build_push_statistics(self, days: int = 7) -> List[Dict[str, Any]]:
        """从每日推送记录中汇总近几天的时段分布和账号排行。"""
        result = []
        now = self._get_utc8_now()
        for offset in range(days - 1, -1, -1):
            date = now - timedelta(days=offset)
            date_str = date.strftime("%Y-%m-%d")
            log_file = self.logs_dir / f"{date.strftime('%Y%m%d')}.log"
            hourly = [0] * 24
            accounts: Dict[str, int] = {}
            total = 0
            if log_file.exists():
                try:
                    with open(log_file, "r", encoding="utf-8") as file:
                        for line in file:
                            try:
                                entry = json.loads(line)
                            except (TypeError, ValueError):
                                continue
                            if not isinstance(entry, dict) or entry.get("type") in {
                                "hotsearch",
                                "initial_snapshot",
                            }:
                                continue
                            time_str = str(entry.get("time", ""))
                            try:
                                hour = datetime.strptime(
                                    time_str, "%Y-%m-%d %H:%M:%S"
                                ).hour
                            except ValueError:
                                continue
                            username = (
                                str(entry.get("username", "未知用户")).strip()
                                or "未知用户"
                            )
                            hourly[hour] += 1
                            accounts[username] = accounts.get(username, 0) + 1
                            total += 1
                except OSError as error:
                    self.plugin_logger.warning(
                        f"读取推送统计日志失败 ({log_file.name}): {error}"
                    )

            result.append(
                {
                    "date": date_str,
                    "total": total,
                    "hourly": hourly,
                    "accounts": [
                        {"username": username, "count": count}
                        for username, count in sorted(
                            accounts.items(), key=lambda item: (-item[1], item[0])
                        )
                    ],
                }
            )
        return result

    async def get_push_statistics(self):
        """供 Plugin Page 查询近七日微博推送统计。"""
        return json_response({"days": self._build_push_statistics()})

    async def save_subscription_mappings(self):
        """校验页面提交的结构化数据，并序列化为兼容的旧字符串列表。"""
        payload = await request.json(default={})
        rows = payload.get("rows") if isinstance(payload, dict) else None
        monitor_urls = (
            payload.get("monitor_urls") if isinstance(payload, dict) else None
        )
        if not isinstance(rows, list):
            return error_response("rows 必须是数组", status_code=400)
        try:
            normalized_monitor_urls = self._normalize_monitored_urls(monitor_urls)
        except ValueError as e:
            return error_response(str(e), status_code=400)

        serialized: List[str] = []
        delivery_options: Dict[str, Dict[str, bool]] = {}
        seen_sessions = set()
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                return error_response(f"第 {index} 行格式无效", status_code=400)
            session_id = str(row.get("session_id", "")).strip()
            mode = row.get("mode")
            raw_uids = row.get("uids", [])
            receive_hotsearch = row.get("receive_hotsearch")
            receive_daily_summary = row.get("receive_daily_summary")
            if not session_id:
                return error_response(
                    f"第 {index} 行的会话 ID 不能为空", status_code=400
                )
            if session_id in seen_sessions:
                return error_response(f"会话 ID 重复：{session_id}", status_code=400)
            seen_sessions.add(session_id)
            if not isinstance(receive_hotsearch, bool) or not isinstance(
                receive_daily_summary, bool
            ):
                return error_response(
                    f"第 {index} 行的热搜或每日总结开关无效", status_code=400
                )
            delivery_options[session_id] = {
                "receive_hotsearch": receive_hotsearch,
                "receive_daily_summary": receive_daily_summary,
            }
            if mode == "all":
                if raw_uids not in ([], None):
                    return error_response(
                        f"第 {index} 行选择“接收全部”时不能填写 UID", status_code=400
                    )
                serialized.append(f"{session_id}: *")
                continue
            if mode != "uids" or not isinstance(raw_uids, list):
                return error_response(f"第 {index} 行的订阅模式无效", status_code=400)

            normalized_uids = []
            for raw_uid in raw_uids:
                uid = str(raw_uid).strip()
                if not uid:
                    continue
                if uid == "*":
                    return error_response(
                        f"第 {index} 行的指定 UID 中不能包含 *", status_code=400
                    )
                if not self._resolve_uid_from_config(uid):
                    return error_response(
                        f"第 {index} 行包含无效 UID 或微博链接：{uid}", status_code=400
                    )
                if uid not in normalized_uids:
                    normalized_uids.append(uid)
            if not normalized_uids:
                return error_response(
                    f"第 {index} 行至少需要一个微博 UID", status_code=400
                )
            serialized.append(f"{session_id}: {', '.join(normalized_uids)}")

        self.config["subscription_mappings"] = serialized
        self.config["subscription_delivery_options"] = delivery_options
        self._set_config("weibo_urls", normalized_monitor_urls)
        if not self._save_subscription_backup(
            serialized, delivery_options, normalized_monitor_urls
        ):
            return error_response(
                "订阅分组备份保存失败，请检查插件数据目录权限后重试", status_code=500
            )
        if not await self._save_plugin_config_async("订阅分组"):
            return error_response(
                "订阅分组未能写入 AstrBot 配置，请重试并查看插件日志", status_code=500
            )
        return json_response(
            {"saved": True, "rows": serialized, "monitor_urls": normalized_monitor_urls}
        )

    def _get_utc8_now(self) -> datetime:
        """获取 UTC+8 时间"""
        return datetime.now(timezone(timedelta(hours=8)))

    def _parse_weibo_time(self, time_str: str) -> str:
        """
        解析微博时间字符串为标准格式 YYYY-MM-DD HH:mm:ss
        """
        if not time_str:
            return self._get_utc8_now().strftime("%Y-%m-%d %H:%M:%S")

        now = self._get_utc8_now()

        try:
            if "刚刚" in time_str:
                return now.strftime("%Y-%m-%d %H:%M:%S")

            if "分钟前" in time_str:
                minutes = int(re.search(r"(\d+)", time_str).group(1))
                res = now - timedelta(minutes=minutes)
                return res.strftime("%Y-%m-%d %H:%M:%S")

            if "小时前" in time_str:
                hours = int(re.search(r"(\d+)", time_str).group(1))
                res = now - timedelta(hours=hours)
                return res.strftime("%Y-%m-%d %H:%M:%S")

            if "昨天" in time_str:
                time_part = re.search(r"(\d{2}:\d{2})", time_str).group(1)
                yesterday = now - timedelta(days=1)
                return f"{yesterday.strftime('%Y-%m-%d')} {time_part}:00"

            if "-" in time_str:
                parts = time_str.split("-")
                if len(parts) == 2:  # MM-DD
                    return f"{now.year}-{time_str} 00:00:00"
                elif len(parts) == 3:  # YYYY-MM-DD
                    return f"{time_str} 00:00:00"

            # 尝试解析微博标准时间格式: Sat Mar 08 16:51:30 +0800 2025
            try:
                dt = datetime.strptime(time_str, "%a %b %d %H:%M:%S %z %Y")
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass

            return time_str
        except Exception as e:
            self.plugin_logger.error(f"解析微博时间失败 ({time_str}): {e}")
            return now.strftime("%Y-%m-%d %H:%M:%S")

    def _log_to_daily_file(
        self,
        post: dict,
        skip_log: bool = False,
        delivery_count: int = 0,
        record_type: str = "weibo",
    ):
        """记录实际推送或初始化快照，使用记录发生时的 UTC+8 时间。"""
        if skip_log or not self._get_config("enable_daily_log", False):
            return

        now = self._get_utc8_now()
        log_file = self.logs_dir / f"{now.strftime('%Y%m%d')}.log"
        log_entry = {
            "type": record_type,
            "time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "published_time": post.get("created_at", ""),
            "username": post.get("username", "未知用户"),
            "content": post.get("text", ""),
            "link": post.get("link", ""),
            "delivery_count": delivery_count,
        }

        try:
            # 检查是否已存在相同的记录（避免重复记录）
            if log_file.exists():
                with open(log_file, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            entry = json.loads(line)
                            if entry.get("link") == post.get("link"):
                                return
                        except (AttributeError, TypeError, json.JSONDecodeError):
                            continue

            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        except Exception as e:
            self.plugin_logger.error(f"记录每日日志失败: {e}")

    def _log_hotsearch_to_daily(self, items: List[dict]):
        """记录热搜推送到每日日志 (JSON 格式)"""
        if not self._get_config("enable_daily_log", False):
            return

        now = self._get_utc8_now()
        date_str = now.strftime("%Y%m%d")
        log_file = self.logs_dir / f"{date_str}.log"

        log_entry = {
            "type": "hotsearch",
            "time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(items),
            "items": [item.get("desc", "") for item in items],
        }

        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        except Exception as e:
            self.plugin_logger.error(f"记录热搜每日日志失败: {e}")

    def _init_last_hotsearch_time(self):
        """检查日志和持久化数据，若 30 分钟内已推送过热搜，初始化 last_hotsearch_time 以避免重载后刷屏"""
        try:
            now = self._get_utc8_now()
            cutoff = now - timedelta(minutes=30)
            last_push_time = None

            for date_offset in [0, 1]:
                check_date = now - timedelta(days=date_offset)
                log_file = self.logs_dir / f"{check_date.strftime('%Y%m%d')}.log"
                if not log_file.exists():
                    continue
                try:
                    with open(log_file, "r", encoding="utf-8") as f:
                        for line in f:
                            try:
                                entry = json.loads(line)
                                if entry.get("type") == "hotsearch":
                                    entry_time = datetime.strptime(
                                        entry["time"], "%Y-%m-%d %H:%M:%S"
                                    ).replace(tzinfo=now.tzinfo)
                                    if entry_time > cutoff and (
                                        last_push_time is None
                                        or entry_time > last_push_time
                                    ):
                                        last_push_time = entry_time
                            except (json.JSONDecodeError, KeyError, ValueError):
                                continue
                except Exception as e:
                    self.plugin_logger.debug(f"读取日志文件 {log_file} 失败: {e}")

            stored_time_str = self._data.get("last_hotsearch_push_time")
            if stored_time_str:
                try:
                    stored_time = datetime.strptime(
                        stored_time_str, "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=now.tzinfo)
                    if stored_time > cutoff and (
                        last_push_time is None or stored_time > last_push_time
                    ):
                        last_push_time = stored_time
                except ValueError:
                    pass

            if last_push_time:
                elapsed = (now - last_push_time).total_seconds()
                loop_time = asyncio.get_event_loop().time()
                self.last_hotsearch_time = loop_time - elapsed
                self.plugin_logger.info(
                    f"检测到最近一次热搜推送在 {elapsed / 60:.1f} 分钟前，已跳过初始推送"
                )
        except Exception as e:
            self.plugin_logger.error(f"初始化热搜推送时间失败: {e}")

    async def _send_daily_summary(self):
        """发送每日总结"""
        if not self._get_config("enable_daily_summary", False):
            return

        now = self._get_utc8_now()
        yesterday = now - timedelta(days=1)
        date_str = yesterday.strftime("%Y%m%d")
        log_file = self.logs_dir / f"{date_str}.log"

        if not log_file.exists():
            self.plugin_logger.info(
                f"未找到昨日 ({date_str}) 的日志文件，跳过每日总结。"
            )
            return

        stats = {}
        hotsearch_count = 0
        has_any_entry = False
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        has_any_entry = True
                        if entry.get("type") == "hotsearch":
                            hotsearch_count += 1
                        else:
                            username = entry.get("username", "未知用户")
                            stats[username] = stats.get(username, 0) + 1
                    except (AttributeError, TypeError, json.JSONDecodeError):
                        continue
        except Exception as e:
            self.plugin_logger.error(f"读取昨日日志文件失败: {e}")
            return

        if not has_any_entry:
            summary_msg = f"📊 微博监控昨日 ({yesterday.strftime('%Y-%m-%d')}) 总结：\n\n昨日未推送任何动态。"
        else:
            summary_lines = [
                f"📊 微博监控昨日 ({yesterday.strftime('%Y-%m-%d')}) 总结：\n"
            ]
            if stats:
                summary_lines.append("📢 微博动态：")
                total = 0
                for user, count in stats.items():
                    summary_lines.append(f"  - {user}: {count} 条")
                    total += count
                summary_lines.append(f"  共计 {total} 条")
            else:
                summary_lines.append("📢 微博动态：无")
            if hotsearch_count > 0:
                summary_lines.append(f"\n🔥 热搜推送：{hotsearch_count} 次")
            summary_msg = "\n".join(summary_lines)

        targets = self.get_delivery_targets("daily_summary")
        if not targets:
            self.plugin_logger.warning("未配置推送目标，无法发送每日总结。")
            return

        chain = MessageChain().message(summary_msg)
        for target in targets:
            try:
                await self.context.send_message(target, chain)
            except Exception as e:
                self.plugin_logger.error(f"发送每日总结到 {target} 失败: {e}")

    async def _fetch_hotsearch(self) -> List[dict]:
        """获取微博热搜榜数据，返回热搜条目列表"""
        try:
            self.plugin_logger.debug("正在获取微博热搜数据...")
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://weibo.com/",
            }

            # 尝试不带 Cookie 获取；429 等待期间不占用请求信号量
            async with self._request_semaphore:
                resp = await self.client.get(HOTSEARCH_API_URL, headers=headers)

            if resp.status_code == 429:
                self.plugin_logger.warning(
                    "获取热搜数据触发限流 (429)，等待 60 秒后重试"
                )
                await asyncio.sleep(60)
                async with self._request_semaphore:
                    resp = await self.client.get(HOTSEARCH_API_URL, headers=headers)

                need_cookie_fallback = False
                data = {}
                if resp.status_code != 200:
                    self.plugin_logger.warning(
                        f"无Cookie获取热搜失败，状态码: {resp.status_code}"
                    )
                    need_cookie_fallback = True
                else:
                    try:
                        data = resp.json()
                        if data.get("ok") != 1:
                            self.plugin_logger.warning("无Cookie热搜接口返回数据异常")
                            need_cookie_fallback = True
                    except Exception as e:
                        self.plugin_logger.warning(f"无Cookie热搜接口解析JSON失败: {e}")
                        need_cookie_fallback = True

                # 如果无 Cookie 获取失败，且配置了 Cookie，则尝试带 Cookie 获取
                if need_cookie_fallback:
                    cookie = self._get_config("weibo_cookie", "")
                    if not cookie:
                        self.plugin_logger.error(
                            "无Cookie获取失败，且未配置 weibo_cookie，无法兜底"
                        )
                        return []

                    self.plugin_logger.info("尝试携带 Cookie 获取热搜数据兜底...")
                    headers["Cookie"] = cookie
                    resp = await self.client.get(HOTSEARCH_API_URL, headers=headers)

                    if resp.status_code != 200:
                        self.plugin_logger.error(
                            f"带Cookie获取热搜数据失败，状态码: {resp.status_code}"
                        )
                        return []

                    try:
                        data = resp.json()
                        if data.get("ok") != 1:
                            self.plugin_logger.error("带Cookie热搜接口返回数据状态异常")
                            return []
                    except Exception as e:
                        self.plugin_logger.error(f"带Cookie热搜接口解析JSON失败: {e}")
                        return []

                realtime = data.get("data", {}).get("realtime", [])
                if not realtime:
                    return []

                filter_ads = self._get_config("hotsearch_filter_ads", True)
                items = []
                for item in realtime:
                    if not isinstance(item, dict):
                        continue
                    if filter_ads and (
                        item.get("is_ad") == 1 or item.get("is_ad_pos") == 1
                    ):
                        self.plugin_logger.debug(
                            f"已过滤广告位热搜: {item.get('word', '')}"
                        )
                        continue

                    word = item.get("word") or item.get("note")
                    if not word:
                        continue

                    heat = str(item.get("num", ""))

                    items.append(
                        {
                            "desc": str(word),
                            "heat": heat,
                            "scheme": f"https://s.weibo.com/weibo?q={quote(word)}",
                        }
                    )

                self.plugin_logger.info(f"成功获取 {len(items)} 条热搜数据")
                return items

        except Exception as e:
            self.plugin_logger.error(f"获取热搜数据出错: {e}")
            return []

    async def _push_hotsearch(self, items: List[dict], targets: List[str]):
        """推送热搜榜到目标会话"""
        if not items:
            self.plugin_logger.debug("热搜条目为空，跳过推送")
            return

        top_n = self._get_config("hotsearch_top_n", DEFAULT_HOTSEARCH_TOP_N)
        display_items = items[:top_n]

        now = self._get_utc8_now()
        time_str = now.strftime("%Y-%m-%d %H:%M")

        show_link = self._get_config("hotsearch_show_link", True)
        item_lines = []
        for idx, item in enumerate(display_items, 1):
            if show_link:
                item_lines.append(f"{idx}. {item['desc']}\n   {item['scheme']}")
            else:
                item_lines.append(f"{idx}. {item['desc']}")

        items_text = "\n\n".join(item_lines)

        template = self._get_config(
            "hotsearch_message_format", DEFAULT_HOTSEARCH_TEMPLATE
        ).replace("\\n", "\n")

        content = template.format(
            top_n=str(len(display_items)),
            time=time_str,
            items=items_text,
        )

        chain = MessageChain().message(content)
        sent_count = 0
        for target in targets:
            try:
                await self.context.send_message(target, chain)
                sent_count += 1
            except Exception as e:
                self.plugin_logger.error(f"推送热搜到 {target} 失败: {e}")

        if sent_count > 0:
            self.plugin_logger.info(
                f"已向 {sent_count}/{len(targets)} 个目标推送热搜榜"
            )
            self._log_hotsearch_to_daily(display_items)
            self._data["last_hotsearch_push_time"] = now.strftime("%Y-%m-%d %H:%M:%S")
            self._save_data()

    def _load_data(self) -> dict:
        """从文件加载持久化数据，损坏时自动备份"""
        if self.data_file.exists():
            try:
                data = json.loads(self.data_file.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError(
                        f"持久化数据顶层类型必须是 dict，实际为 {type(data).__name__}"
                    )
                return data
            except Exception as e:
                self.plugin_logger.error(f"WeiboMonitor: 加载数据文件失败: {e}")
                # 自动备份损坏的文件
                try:
                    backup_file = self.data_file.with_suffix(
                        f".bak.{int(asyncio.get_event_loop().time())}"
                    )
                    self.data_file.rename(backup_file)
                    self.plugin_logger.info(
                        f"WeiboMonitor: 已将损坏的数据文件备份为 {backup_file}"
                    )
                except Exception as backup_err:
                    self.plugin_logger.error(
                        f"WeiboMonitor: 备份损坏的数据文件失败: {backup_err}"
                    )
        return {}

    def _save_data(self):
        """将持久化数据保存到文件（原子写入，避免数据损坏）"""
        try:
            # 先写入临时文件，成功后再替换原文件，防止写入中断导致数据损坏
            temp_file = self.data_file.with_suffix(".tmp")
            temp_file.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=4), encoding="utf-8"
            )
            # 原子替换
            temp_file.replace(self.data_file)
            return True
        except Exception as e:
            self.plugin_logger.error(f"WeiboMonitor: 保存数据文件失败: {e}")
            # 清理临时文件
            try:
                if temp_file.exists():
                    temp_file.unlink()
            except OSError:
                pass
            return False

    async def get_kv_data(self, key: str, default=None):
        """获取持久化键值对"""
        return self._data.get(key, default)

    async def put_kv_data(self, key: str, value):
        """设置并保存持久化键值对"""
        self._data[key] = value
        self._save_data()

    def _migrate_config_v2(self):
        """将 target_conversation_id 迁移到统一的 subscription_mappings 格式。
        核心迁移逻辑同步执行（修改 self.config），确保 run_monitor 启动前已完成。
        save_config 持久化抛后异步执行，失败不影响运行。
        """
        targets = self.get_targets_legacy()
        if not targets:
            return

        mappings = self.config.get("subscription_mappings", [])
        if isinstance(mappings, str):
            mappings = [item.strip() for item in mappings.splitlines() if item.strip()]
        elif not isinstance(mappings, list):
            mappings = []
        subscribed = self._get_all_subscribed_sessions()

        added = False
        for target in targets:
            if target not in subscribed:
                mappings.append(f"{target}: *")
                added = True
                self.plugin_logger.info(f"配置迁移: {target} → {target}: *")

        if added:
            self.config["subscription_mappings"] = mappings
            self.config["target_conversation_id"] = []
            # 异步持久化到框架
            self._migrate_persist_task = asyncio.create_task(
                self._persist_migrated_config()
            )

    async def _persist_migrated_config(self):
        """异步持久化迁移后的配置到框架存储。"""
        await self._save_plugin_config_async("配置迁移后")

    def get_targets_legacy(self) -> List[str]:
        """读取旧版 target_conversation_id 配置（仅用于迁移）"""
        targets_raw = self.config.get("target_conversation_id", [])
        if isinstance(targets_raw, str):
            return [t.strip() for t in targets_raw.split(",") if t.strip()]
        targets = []
        if isinstance(targets_raw, list):
            for item in targets_raw:
                item_str = str(item).strip()
                if "," in item_str:
                    targets.extend(
                        [t.strip() for t in item_str.split(",") if t.strip()]
                    )
                elif item_str:
                    targets.append(item_str)
        return targets

    def get_headers(self, uid: str = "") -> Dict[str, str]:
        """获取请求头"""
        cookie = self._get_config("weibo_cookie", "")
        headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        }
        if uid:
            headers["Referer"] = f"{WEIBO_MOBILE_BASE}/u/{uid}"
        else:
            headers["Referer"] = f"{WEIBO_MOBILE_BASE}/"

        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def terminate(self):
        self.running = False
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
        if self.push_consumer_task:
            self.push_consumer_task.cancel()
            try:
                await self.push_consumer_task
            except asyncio.CancelledError:
                pass
        if self._migrate_persist_task:
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._migrate_persist_task), timeout=5
                )
            except asyncio.TimeoutError:
                self.plugin_logger.warning(
                    "配置迁移保存超过 5 秒，停止等待并取消保存任务"
                )
                self._migrate_persist_task.cancel()
                try:
                    await self._migrate_persist_task
                except asyncio.CancelledError:
                    pass
            except Exception as e:
                self.plugin_logger.warning(f"等待配置迁移保存时出错: {e}")
        pending_pushes = self.push_queue.qsize()
        if pending_pushes:
            self.plugin_logger.warning(
                f"插件停止时推送队列仍有 {pending_pushes} 条待处理消息，将不再发送"
            )
        await self.client.aclose()
        self.plugin_logger.info("WeiboMonitor 插件已停止")

    def _extract_image_urls(self, mblog: dict) -> List[str]:
        """从微博博文数据中提取高清图片 URL 列表"""
        image_urls = []
        pics = mblog.get("pics") or []
        for pic in pics:
            if not isinstance(pic, dict):
                continue
            large = pic.get("large") or {}
            url = large.get("url") or pic.get("url")
            if url:
                if url.startswith("//"):
                    url = "https:" + url
                image_urls.append(url)
        return image_urls

    def _extract_video_info(self, mblog: dict) -> Optional[dict]:
        """从微博博文数据中提取视频信息。
        若 page_info.type == "video" 则返回包含 url、cover 等的字典，
        否则返回 None。同时检查转发微博的视频。
        """
        page_info = mblog.get("page_info")
        if not (
            page_info
            and isinstance(page_info, dict)
            and page_info.get("type") == "video"
        ):
            retweet = mblog.get("retweeted_status")
            if isinstance(retweet, dict):
                page_info = retweet.get("page_info")
        if not (
            page_info
            and isinstance(page_info, dict)
            and page_info.get("type") == "video"
        ):
            return None

        urls = page_info.get("urls") or {}
        video_url = None
        for quality in ("mp4_720p_mp4", "mp4_hd_mp4", "mp4_ld_mp4"):
            video_url = urls.get(quality)
            if video_url:
                break
        if not video_url:
            return None

        if video_url.startswith("//"):
            video_url = "https:" + video_url

        return {
            "url": video_url,
            "cover": page_info.get("page_pic"),
            "duration": (page_info.get("media_info") or {}).get("duration"),
            "title": page_info.get("page_title"),
        }

    async def _download_image(self, url: str, save_name: str) -> Optional[str]:
        """下载图片到临时目录，返回本地文件路径"""
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1",
                "Referer": "https://m.weibo.cn/",
            }
            cookie = self._get_config("weibo_cookie", "")
            if cookie:
                headers["Cookie"] = cookie
            async with self._request_semaphore:
                resp = await self.client.get(url, headers=headers)
                if resp.status_code == 200 and len(resp.content) > 0:
                    save_path = self.temp_images_dir / save_name
                    save_path.write_bytes(resp.content)
                    return str(save_path)
                else:
                    self.plugin_logger.warning(
                        f"下载图片失败，状态码: {resp.status_code}，URL: {url}"
                    )
                    return None
        except Exception as e:
            self.plugin_logger.error(f"下载图片出错: {e}，URL: {url}")
            return None

    def _cleanup_temp_media(self):
        """清理临时媒体目录中超过配置保留时长的文件。0 = 不清理。"""
        retention = self._get_config("temp_media_retention_minutes", 10)
        if retention <= 0:
            return
        try:
            import time

            now = time.time()
            max_age = retention * 60
            for f in self.temp_images_dir.iterdir():
                if f.is_file() and now - f.stat().st_mtime > max_age:
                    f.unlink()
        except Exception as e:
            self.plugin_logger.debug(f"清理临时媒体文件出错: {e}")

    async def _download_video(self, url: str, save_name: str) -> Optional[str]:
        """流式下载视频到临时目录，返回本地文件路径。
        受 max_video_size_mb 限制（0 = 不限制）。
        """
        max_size_mb = self._get_config("max_video_size_mb", 0)
        max_bytes = max_size_mb * 1024 * 1024 if max_size_mb > 0 else None
        headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1",
            "Referer": "https://m.weibo.cn/",
        }
        cookie = self._get_config("weibo_cookie", "")
        if cookie:
            headers["Cookie"] = cookie

        try:
            async with self._request_semaphore:
                async with self.client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code != 200:
                        self.plugin_logger.warning(
                            f"下载视频失败，状态码: {resp.status_code}，URL: {url}"
                        )
                        return None

                    content_length = resp.headers.get("content-length")
                    if content_length and max_bytes:
                        size_mb = int(content_length) / (1024 * 1024)
                        if size_mb > max_size_mb:
                            self.plugin_logger.warning(
                                f"视频大小 {size_mb:.1f}MB 超过限制 {max_size_mb}MB，跳过: {url}"
                            )
                            return None

                    save_path = self.temp_images_dir / save_name
                    downloaded = 0
                    with open(save_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            f.write(chunk)
                            if max_bytes:
                                downloaded += len(chunk)
                                if downloaded > max_bytes:
                                    f.close()
                                    try:
                                        save_path.unlink(missing_ok=True)
                                    except Exception:
                                        pass
                                    self.plugin_logger.warning(
                                        f"视频下载超过限制 {max_size_mb}MB，已取消: {url}"
                                    )
                                    return None
                    return str(save_path)
        except Exception as e:
            self.plugin_logger.error(f"下载视频出错: {e}，URL: {url}")
            return None

    def _format_post_text(self, post: dict, msg_format: str) -> str:
        """格式化微博文本内容"""
        return msg_format.format(
            name=post.get("username", "未知用户"),
            weibo=post["text"],
            link=post["link"],
        )

    async def _download_post_images(self, post: dict) -> List[str]:
        """下载微博图片并返回本地路径列表"""
        image_urls = post.get("image_urls", [])
        max_images = self._get_config("max_images_per_post", 0)
        if max_images > 0:
            image_urls = image_urls[:max_images]

        local_paths = []
        for idx, url in enumerate(image_urls):
            ext = "jpg"
            if ".png" in url.lower():
                ext = "png"
            elif ".gif" in url.lower():
                ext = "gif"
            elif ".webp" in url.lower():
                ext = "webp"
            uid_part = (
                post.get("link", "unknown").split("/")[-1]
                if post.get("link")
                else "unknown"
            )
            save_name = f"{uid_part}_{idx}.{ext}"
            local_path = await self._download_image(url, save_name)
            if local_path:
                local_paths.append(local_path)
        return local_paths

    async def _download_post_video(self, post: dict) -> Optional[str]:
        """下载微博视频并返回本地路径。无视频时返回 None。"""
        video_info = post.get("video_info")
        if not video_info:
            return None
        video_url = video_info.get("url")
        if not video_url:
            return None
        uid_part = (
            post.get("link", "unknown").split("/")[-1]
            if post.get("link")
            else "unknown"
        )
        save_name = f"{uid_part}_video.mp4"
        return await self._download_video(video_url, save_name)

    async def _send_post_to_targets(
        self, post: dict, msg_format: str, targets: List[str], skip_log: bool = False
    ) -> int:
        """发送单条微博到指定目标。
        文字与图片分别独立发送，解决飞书适配器图文混合消息文字丢失问题（统一应用于所有平台）。
        返回实际发送的图片数量。
        """
        text_content = self._format_post_text(post, msg_format)

        # 图片下载和推送
        image_paths: List[str] = []
        if self._get_config("enable_image_download", True):
            image_paths = await self._download_post_images(post)

        # 文字与图片分别独立发送，解决飞书适配器图文混合消息文字丢失问题。
        # 所有平台统一采用此方式。
        text_chain = MessageChain().message(text_content)

        if image_paths:
            img_chain = MessageChain()
            for img_path in image_paths:
                img_chain.chain.append(Comp.Image(file=img_path))

        successful_targets = 0
        for target in targets:
            try:
                await self.context.send_message(target, text_chain)
                successful_targets += 1
                if image_paths:
                    await self.context.send_message(target, img_chain)
            except Exception as e:
                self.plugin_logger.error(f"WeiboMonitor: 推送到 {target} 失败: {e}")

        if successful_targets:
            self._data["last_push_time"] = self._get_utc8_now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            self._save_data()
            if not skip_log:
                self._log_to_daily_file(post, delivery_count=successful_targets)

        # 视频下载和推送
        video_path = None
        if self._get_config("enable_video_download", True) and post.get("video_info"):
            self.plugin_logger.info(
                f"检测到视频微博，开始下载: {post.get('link', 'unknown')}"
            )
            try:
                dl_timeout = self._get_config("video_download_timeout", 60)
                if dl_timeout > 0:
                    video_path = await asyncio.wait_for(
                        self._download_post_video(post), timeout=dl_timeout
                    )
                else:
                    video_path = await self._download_post_video(post)
            except asyncio.TimeoutError:
                self.plugin_logger.warning(
                    f"视频下载超时（{dl_timeout}秒），已跳过: {post.get('link', 'unknown')}"
                )
                video_path = None
        post["_video_sent"] = video_path is not None

        if video_path:
            send_timeout = self._get_config("video_send_timeout", 60)
            video_chain = MessageChain()
            video_chain.chain.append(Comp.Video.fromFileSystem(path=video_path))
            for target in targets:
                try:
                    if send_timeout > 0:
                        await asyncio.wait_for(
                            self.context.send_message(target, video_chain),
                            timeout=send_timeout,
                        )
                    else:
                        await self.context.send_message(target, video_chain)
                except asyncio.TimeoutError:
                    self.plugin_logger.warning(
                        f"视频推送到 {target} 超时（{send_timeout}秒）"
                    )
                except Exception as e:
                    self.plugin_logger.error(
                        f"WeiboMonitor: 视频推送到 {target} 失败: {e}"
                    )

        return len(image_paths)

    @staticmethod
    def _parse_bid_from_url(url: str) -> Optional[Tuple[str, Optional[str]]]:
        """从微博链接中解析 bid 和可选的 uid。
        支持格式：
        - https://weibo.com/uid/bid
        - https://m.weibo.cn/detail/bid
        - https://m.weibo.cn/status/bid
        - https://weibo.com/detail/bid
        返回 (bid, uid) 或 None
        """
        url = url.strip()
        match = re.search(r"weibo\.(com|cn)/(\d+)/([A-Za-z0-9]+)", url)
        if match:
            return (match.group(3), match.group(2))
        match2 = re.search(r"weibo\.(com|cn)/(detail|status)/([A-Za-z0-9]+)", url)
        if match2:
            return (match2.group(3), None)
        match3 = re.search(r"weibo\.(com|cn)/[^/]+/([A-Za-z0-9]+)", url)
        if match3:
            return (match3.group(2), None)
        return None

    async def _fetch_single_weibo(self, bid: str) -> Optional[dict]:
        """通过 bid 抓取单条微博详情，返回 post dict 或 None"""
        try:
            api_url = f"{WEIBO_MOBILE_BASE}/statuses/show?id={bid}"
            async with self._request_semaphore:
                resp = await self.client.get(api_url, headers=self.get_headers())
                if resp.status_code != 200:
                    self.plugin_logger.warning(
                        f"获取单条微博失败，状态码: {resp.status_code}，bid: {bid}"
                    )
                    return None
                data = resp.json()
                if data.get("ok") != 1:
                    self.plugin_logger.warning(f"获取单条微博数据异常，bid: {bid}")
                    return None
                mblog = data.get("data")
                if not mblog or not isinstance(mblog, dict):
                    return None

                uid = (mblog.get("user") or {}).get("idstr") or str(
                    (mblog.get("user") or {}).get("id", "")
                )
                username = (mblog.get("user") or {}).get("screen_name", "未知用户")
                text = self.clean_text(mblog.get("text", ""))
                link = (
                    f"{WEIBO_WEB_BASE}/{uid}/{bid}"
                    if uid
                    else f"{WEIBO_WEB_BASE}/detail/{bid}"
                )
                created_at = self._parse_weibo_time(mblog.get("created_at", ""))
                image_urls = self._extract_image_urls(mblog)
                video_info = self._extract_video_info(mblog)

                return {
                    "text": text,
                    "link": link,
                    "username": username,
                    "created_at": created_at,
                    "image_urls": image_urls,
                    "video_info": video_info,
                }
        except Exception as e:
            self.plugin_logger.error(f"抓取单条微博出错: {e}，bid: {bid}")
            return None

    def _iter_mappings(self):
        """统一解析 subscription_mappings，自动规范化缺失的 :*。
        对只写了会话 ID 而省略冒号的行，自动补全为 *。
        对只写了冒号但右侧为空的行，自动补全为 *。
        """
        mappings = self.config.get("subscription_mappings", [])
        if isinstance(mappings, str):
            mappings = [m.strip() for m in mappings.split("\n") if m.strip()]
        if not isinstance(mappings, list):
            return
        for mapping in mappings:
            mapping = str(mapping).strip()
            if not mapping:
                continue
            if ":" not in mapping:
                # 用户只写了会话ID，自动补全为 *（接收全部）
                yield (mapping, "*")
                continue
            parsed = self._split_subscription_mapping(mapping)
            if not parsed:
                continue
            session_id, uids_str = parsed
            if not uids_str:
                # 用户写了 "会话ID:" 但后面为空，自动补全为 *
                uids_str = "*"
            yield (session_id, uids_str)

    def get_targets(self) -> List[str]:
        """返回所有接收全部微博动态的会话。"""
        targets = []
        for session_id, uids_str in self._iter_mappings():
            if uids_str == "*":
                targets.append(session_id)
        return targets

    def get_delivery_targets(self, delivery_type: str) -> List[str]:
        """返回勾选接收热搜或每日总结的会话，兼容旧版 * 的默认接收规则。"""
        option_key = {
            "hotsearch": "receive_hotsearch",
            "daily_summary": "receive_daily_summary",
        }.get(delivery_type)
        if not option_key:
            raise ValueError(f"未知的推送类型: {delivery_type}")

        targets = []
        for session_id, uids_str in self._iter_mappings():
            options = self._get_delivery_options(session_id, uids_str == "*")
            if options[option_key]:
                targets.append(session_id)
        return targets

    def _get_all_subscribed_sessions(self) -> set:
        """返回 subscription_mappings 中所有已配置的会话 ID。"""
        sessions = set()
        for session_id, _ in self._iter_mappings():
            sessions.add(session_id)
        return sessions

    def _get_targets_for_uid(self, uid: str) -> List[str]:
        """返回应接收指定 UID 微博推送的所有会话。
        * 表示接收全部，匹配具体 UID 则只推送给该会话。
        """
        targets = set()
        for session_id, uids_str in self._iter_mappings():
            if uids_str == "*":
                targets.add(session_id)
                continue
            for sub_item in uids_str.split(","):
                sub_item = sub_item.strip()
                if self._resolve_uid_from_config(sub_item) == uid:
                    targets.add(session_id)
                    break
        return list(targets)

    @staticmethod
    def _resolve_uid_from_config(item: str) -> Optional[str]:
        """从配置条目中提取数字 UID，支持纯数字或 URL 格式。"""
        item = item.strip()
        if item.isdigit():
            return item
        match = re.search(r"weibo\.(com|cn)/u/(\d+)", item)
        if match:
            return match.group(2)
        return None

    @filter.command("weibo_umo")
    async def weibo_umo(self, event: AstrMessageEvent):
        """获取当前会话 ID，并给出配置示例"""
        sid = event.unified_msg_origin
        yield event.plain_result(
            f"📌 当前会话 ID: {sid}\n\n"
            f"请在 WebUI 的本插件详情页打开“订阅分组管理”页面后添加：\n\n"
            f"  接收所有已监控博主的微博:\n"
            f"    {sid}: *\n\n"
            f"  只接收指定博主：在页面的勾选列表中选择已监控账号。\n\n"
            f"热搜和每日总结可在同一行的勾选框中独立开启。博主 UID 从微博主页链接中获取，如 https://weibo.com/u/1234567890 → UID 是 1234567890"
        )

    @filter.command("weibo_export")
    async def weibo_export(self, event: AstrMessageEvent):
        """导出当前插件配置"""
        try:
            command = self._build_export_command()
            config_b64 = command.split(" ", 1)[1]
            yield event.plain_result(
                f"📦 WeiboMonitor 配置导出成功 (Base64格式):\n\n{config_b64}\n\n"
                f"💡 请妥善保管此字符串，在其他会话或环境中使用 /weibo_import [配置字符串] 即可导入。"
            )
        except Exception as e:
            self.plugin_logger.error(f"WeiboMonitor: 导出配置失败: {e}")
            yield event.plain_result(f"❌ 导出配置失败: {e}")

    @filter.command("weibo_import")
    async def weibo_import(self, event: AstrMessageEvent, config_str: str = ""):
        """从导出的字符串导入配置"""
        message_str: str = event.message_str or ""
        if message_str:
            parts = message_str.split(maxsplit=1)
            if len(parts) > 1:
                config_str = parts[1].strip()

        if not config_str:
            yield event.plain_result(
                "❌ 请提供配置字符串。用法: /weibo_import <配置字符串>"
            )
            return

        try:
            # 兼容直接 JSON 或 Base64
            try:
                decoded = base64.b64decode(config_str).decode("utf-8")
                new_config = json.loads(decoded)
            except Exception:
                new_config = json.loads(config_str)

            if not isinstance(new_config, dict):
                raise ValueError("配置格式不正确")

            # 兼容性合并：保持当前版本已有的键，仅更新导入的键
            # 即使未来增加了更多配置项，此导入逻辑依然稳健
            count = 0
            for key, value in new_config.items():
                self.config[key] = value
                count += 1

            imported_groups = any(group in new_config for group in CONFIG_GROUPS)
            if not imported_groups and any(
                key in CONFIG_KEY_GROUPS for key in new_config
            ):
                self.config["_config_schema_version"] = 0
                self._migrate_grouped_config()

            # 尝试重新设置日志（如果配置有变）
            self.setup_logging()

            # 尝试调用框架的配置保存接口（如果支持）
            try:
                if hasattr(self.context, "config_manager") and hasattr(
                    self.context.config_manager, "save_config"
                ):
                    self.context.config_manager.save_config()
            except Exception:
                pass

            # 兜底：如果导入的配置包含 Cookie，同步写入 _data 持久化文件
            imported_cookie = self._get_config("weibo_cookie", "")
            if imported_cookie:
                self._set_cookie_health_status("unknown")
                self._data["_backup_weibo_cookie"] = imported_cookie
                self._save_data()

            yield event.plain_result(
                f"✅ 成功导入 {count} 项配置！\n"
                f"注意：部分配置（如检查间隔）可能需要重启插件后才能完全生效。导入后请先刷新插件后台页面，否则配置无法显示。"
            )
        except Exception as e:
            self.plugin_logger.error(f"WeiboMonitor: 导入配置失败: {e}")
            yield event.plain_result(f"❌ 导入配置失败: {e}")

    @filter.command("weibo_verify")
    async def weibo_verify(self, event: AstrMessageEvent):
        """验证当前配置的 Cookie 是否有效"""
        cookie = self._get_config("weibo_cookie", "")
        if not cookie:
            yield event.plain_result("❌ 未配置 Cookie。")
            return

        yield event.plain_result("🔍 正在验证 Cookie 有效性...")
        try:
            resp = await self.client.get(
                "https://m.weibo.cn/api/config", headers=self.get_headers()
            )
            if resp.status_code == 200:
                data = resp.json()
                data_obj = data.get("data") or {}
                if data_obj.get("login"):
                    self._set_cookie_health_status("valid")
                    user = data_obj.get("user")
                    if user:
                        yield event.plain_result(
                            f"✅ Cookie 有效！\n当前登录用户: {user.get('screen_name')} (UID: {user.get('id')})"
                        )
                    else:
                        uid = data_obj.get("uid")
                        yield event.plain_result(
                            f"✅ Cookie 有效！\n已登录但未获取到详细用户信息 (UID: {uid})"
                        )
                else:
                    self._set_cookie_health_status("invalid")
                    yield event.plain_result(
                        "❌ Cookie 已失效或未登录（接口返回 login: false）。"
                    )
            else:
                self._set_cookie_health_status("invalid")
                yield event.plain_result(f"❌ 验证请求失败，状态码: {resp.status_code}")
        except Exception as e:
            self._set_cookie_health_status("invalid")
            self.plugin_logger.error(f"WeiboMonitor: 验证过程中出现错误: {e}")
            yield event.plain_result(f"❌ 验证过程中出现错误: {e}")

    @filter.command("weibo_cookie")
    async def weibo_cookie(self, event: AstrMessageEvent, cookie: str = ""):
        """更换微博 Cookie 并自动重载插件"""
        message_str: str = event.message_str or ""
        if message_str:
            parts = message_str.split(maxsplit=1)
            if len(parts) > 1:
                cookie = parts[1].strip()

        if not cookie:
            yield event.plain_result(
                "❌ 请提供 Cookie。用法: /weibo_cookie <Cookie字符串>"
            )
            return

        self._set_config("weibo_cookie", cookie)
        self.cookie_invalid_notified = False
        self._set_cookie_health_status("unknown")

        try:
            if hasattr(self.context, "config_manager") and hasattr(
                self.context.config_manager, "save_config"
            ):
                self.context.config_manager.save_config()
                saved = True
            else:
                saved = False
        except Exception as e:
            self.plugin_logger.error(f"WeiboMonitor: 保存配置失败: {e}")
            saved = False

        # 兜底：将 Cookie 写入 _data 持久化文件，防止框架配置保存失败时丢失
        self._data["_backup_weibo_cookie"] = cookie
        self._save_data()
        if not saved:
            saved = True

        yield event.plain_result("🔄 Cookie 已更新，正在验证有效性...")
        try:
            resp = await self.client.get(
                "https://m.weibo.cn/api/config", headers=self.get_headers()
            )
            if resp.status_code == 200:
                data = resp.json()
                data_obj = data.get("data") or {}
                if data_obj.get("login"):
                    self._set_cookie_health_status("valid")
                    user = data_obj.get("user")
                    user_info = (
                        f"当前登录用户: {user.get('screen_name')} (UID: {user.get('id')})"
                        if user
                        else f"已登录 (UID: {data_obj.get('uid')})"
                    )
                    save_msg = (
                        "✅ 配置已持久化保存"
                        if saved
                        else "⚠️ 配置已更新但未能持久化保存，重启后可能丢失"
                    )
                    self.plugin_logger.info(
                        f"WeiboMonitor: Cookie 已通过命令更换，{save_msg}"
                    )
                    yield event.plain_result(
                        f"✅ Cookie 更换成功！{user_info}\n{save_msg}\n"
                        f"🔄 正在重载插件..."
                    )
                    try:
                        if hasattr(self.context, "star_loader") and hasattr(
                            self.context.star_loader, "reload"
                        ):
                            self.context.star_loader.reload(
                                "astrbot_plugin_weibo_monitor"
                            )
                        elif hasattr(self.context, "reload_plugin"):
                            self.context.reload_plugin("astrbot_plugin_weibo_monitor")
                        else:
                            yield event.plain_result(
                                "⚠️ 无法自动重载插件，请手动在 WebUI 插件管理中点击「重载插件」，或重启 AstrBot。\n💡 新 Cookie 已生效，无需重载亦可正常使用。"
                            )
                    except Exception as reload_err:
                        self.plugin_logger.warning(
                            f"WeiboMonitor: 自动重载插件失败: {reload_err}"
                        )
                        yield event.plain_result(
                            "⚠️ 自动重载失败，请手动在 WebUI 插件管理中点击「重载插件」。\n💡 新 Cookie 已生效，无需重载亦可正常使用。"
                        )
                else:
                    self._set_cookie_health_status("invalid")
                    yield event.plain_result(
                        "❌ Cookie 已更新但验证失败（接口返回 login: false），请检查 Cookie 是否正确。"
                    )
            else:
                self._set_cookie_health_status("invalid")
                yield event.plain_result(
                    f"❌ Cookie 已更新但验证请求失败，状态码: {resp.status_code}"
                )
        except Exception as e:
            self._set_cookie_health_status("invalid")
            self.plugin_logger.error(f"WeiboMonitor: 更换 Cookie 后验证出错: {e}")
            yield event.plain_result(f"❌ Cookie 已更新但验证过程出错: {e}")

    @filter.command("weibo_check")
    async def weibo_check(self, event: AstrMessageEvent):
        """立即抓取列表里第一个账号并推送最新一条微博"""
        urls = self._parse_urls(self._get_config("weibo_urls", []))
        if not urls:
            yield event.plain_result("❌ 未在插件设置中配置监控URL。")
            return

        yield event.plain_result("🔍 正在检查首个微博账号的最新动态...")

        url = urls[0]
        msg_format = self.message_format

        uid = await self.parse_uid(url)
        if not uid:
            yield event.plain_result(f"❌ 无法解析URL: {url}")
            return

        latest_posts = await self.check_weibo(uid, force_fetch=True)
        if latest_posts:
            uid_targets = self._get_targets_for_uid(uid)
            await self._send_new_posts(
                latest_posts,
                uid_targets,
                msg_format,
                event.unified_msg_origin,
                skip_log=True,
            )
            yield event.plain_result(
                f"✅ {latest_posts[0].get('username')} 已发送最新动态。"
            )
        else:
            yield event.plain_result(f"ℹ️ UID {uid} 未获取到有效微博。")

    @filter.command("weibo_check_all")
    async def weibo_check_all(self, event: AstrMessageEvent):
        """立即抓取列表里所有账号并推送最新微博（逐个检查，间隔请求）"""
        urls = self._parse_urls(self._get_config("weibo_urls", []))
        msg_format = self.message_format

        base_req_interval = self._get_config(
            "request_interval", DEFAULT_REQUEST_INTERVAL
        )
        req_jitter = self._get_config("request_interval_jitter", 0)

        if not urls:
            yield event.plain_result("❌ 未在插件设置中配置监控URL。")
            return

        yield event.plain_result(f"🔍 正在立即检查 {len(urls)} 个微博账号的最新动态...")

        results = []
        for i, url in enumerate(urls):
            if i > 0:
                actual_req_interval = max(
                    1,
                    random.randint(
                        base_req_interval - req_jitter, base_req_interval + req_jitter
                    ),
                )
                await asyncio.sleep(actual_req_interval)

            uid = await self.parse_uid(url)
            if not uid:
                results.append(f"❌ 无法解析URL: {url}")
                continue

            latest_posts = await self.check_weibo(uid, force_fetch=True)
            if latest_posts:
                uid_targets = self._get_targets_for_uid(uid)
                await self._send_new_posts(
                    latest_posts,
                    uid_targets,
                    msg_format,
                    event.unified_msg_origin,
                    skip_log=True,
                )
                results.append(f"✅ {latest_posts[0].get('username')} 已发送最新动态。")
            else:
                results.append(f"ℹ️ UID {uid} 未获取到有效微博。")

        yield event.plain_result("\n".join(results))

    @filter.command("weibo_hot")
    async def weibo_hot(self, event: AstrMessageEvent):
        """手动查询当前微博热搜榜"""
        if not self._get_config("enable_hotsearch", False):
            yield event.plain_result("❌ 热搜监控功能未开启，请先在插件设置中启用。")
            return

        targets = self.get_delivery_targets("hotsearch")
        if not targets:
            targets = [event.unified_msg_origin]

        yield event.plain_result("🔥 正在获取微博热搜榜...")
        try:
            hot_items = await self._fetch_hotsearch()
            if hot_items:
                await self._push_hotsearch(hot_items, targets)
                yield event.plain_result("✅ 热搜榜已发送。")
            else:
                yield event.plain_result("❌ 未获取到热搜数据，请稍后重试。")
        except Exception as e:
            self.plugin_logger.error(f"手动查询热搜出错: {e}")
            yield event.plain_result(f"❌ 查询热搜失败: {e}")

    @filter.command("weibo_status")
    async def weibo_status(self, event: AstrMessageEvent):
        """查看当前监控状态（账号数、推送目标、Cookie、检查间隔等）"""
        urls = self._parse_urls(self._get_config("weibo_urls", []))
        all_sessions = self._get_all_subscribed_sessions()
        hotsearch_sessions = set(self.get_delivery_targets("hotsearch"))
        summary_sessions = set(self.get_delivery_targets("daily_summary"))
        star_sessions = set(self.get_targets())
        specific_sessions = all_sessions - star_sessions

        status_lines = ["📊 微博监控当前状态："]
        status_lines.append(f"- 监控账号数：{len(urls)} 个")

        if all_sessions:
            status_lines.append(f"- 推送目标数：{len(all_sessions)} 个会话")
            if star_sessions:
                status_lines.append(
                    f"  （{len(star_sessions)} 个全局会话（*），{len(specific_sessions)} 个订阅会话）"
                )
        else:
            status_lines.append("- 推送目标：未配置（所有推送将不发送）")

        check_interval = self._get_config("check_interval", DEFAULT_CHECK_INTERVAL)
        status_lines.append(f"- 检查间隔：{check_interval} 分钟")

        cookie = self._get_config("weibo_cookie", "")
        cookie_status = "✅ 已配置" if cookie else "❌ 未配置"
        status_lines.append(f"- Cookie：{cookie_status}")

        has_active_push = bool(all_sessions and cookie)
        status_lines.append(
            f"- 自动推送：{'✅ 开启' if has_active_push else '❌ 关闭'}"
        )

        daily_summary = self._get_config("enable_daily_summary", False)
        if daily_summary:
            summary_time = self._get_config("daily_summary_time", "08:00")
            status_lines.append(
                f"- 每日总结：✅ 开启 ({summary_time}，{len(summary_sessions)} 个接收会话)"
            )
        else:
            status_lines.append("- 每日总结：❌ 关闭")

        hotsearch_enabled = self._get_config("enable_hotsearch", False)
        if hotsearch_enabled:
            hotsearch_interval = self._get_config(
                "hotsearch_interval", DEFAULT_HOTSEARCH_INTERVAL
            )
            hotsearch_top_n = self._get_config(
                "hotsearch_top_n", DEFAULT_HOTSEARCH_TOP_N
            )
            status_lines.append(
                f"- 热搜监控：✅ 开启 (每 {hotsearch_interval} 分钟, Top {hotsearch_top_n}，{len(hotsearch_sessions)} 个接收会话)"
            )
        else:
            status_lines.append("- 热搜监控：❌ 关闭")

        if urls:
            status_lines.append("\n📋 监控列表：")
            for i, url in enumerate(urls[:5], 1):
                status_lines.append(f"  {i}. {url}")
            if len(urls) > 5:
                status_lines.append(f"  ... 等共 {len(urls)} 个")

        yield event.plain_result("\n".join(status_lines))

    @filter.command("weibo_summary")
    async def weibo_summary(self, event: AstrMessageEvent):
        """手动触发昨日总结推送"""
        if not self._get_config("enable_daily_summary", False):
            yield event.plain_result("❌ 每日总结功能未开启，请先在插件设置中启用。")
            return

        targets = self.get_delivery_targets("daily_summary")
        if not targets:
            yield event.plain_result("❌ 未配置推送目标，无法发送每日总结。")
            return

        yield event.plain_result("📊 正在生成昨日总结...")

        now = self._get_utc8_now()
        yesterday = now - timedelta(days=1)
        date_str = yesterday.strftime("%Y%m%d")
        log_file = self.logs_dir / f"{date_str}.log"

        if not log_file.exists():
            yield event.plain_result(
                f"ℹ️ 未找到昨日 ({yesterday.strftime('%Y-%m-%d')}) 的推送记录。"
            )
            return

        stats = {}
        hotsearch_count = 0
        has_any_entry = False
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        has_any_entry = True
                        if entry.get("type") == "hotsearch":
                            hotsearch_count += 1
                        else:
                            username = entry.get("username", "未知用户")
                            stats[username] = stats.get(username, 0) + 1
                    except (AttributeError, TypeError, json.JSONDecodeError):
                        continue
        except Exception as e:
            self.plugin_logger.error(f"读取昨日日志文件失败: {e}")
            yield event.plain_result(f"❌ 读取日志失败: {e}")
            return

        if not has_any_entry:
            summary_msg = f"📊 微博监控昨日 ({yesterday.strftime('%Y-%m-%d')}) 总结：\n\n昨日未推送任何动态。"
        else:
            summary_lines = [
                f"📊 微博监控昨日 ({yesterday.strftime('%Y-%m-%d')}) 总结：\n"
            ]
            if stats:
                summary_lines.append("📢 微博动态：")
                total = 0
                for user, count in stats.items():
                    summary_lines.append(f"  - {user}: {count} 条")
                    total += count
                summary_lines.append(f"  共计 {total} 条")
            else:
                summary_lines.append("📢 微博动态：无")
            if hotsearch_count > 0:
                summary_lines.append(f"\n🔥 热搜推送：{hotsearch_count} 次")
            summary_msg = "\n".join(summary_lines)

        chain = MessageChain().message(summary_msg)
        success_count = 0
        for target in targets:
            try:
                await self.context.send_message(target, chain)
                success_count += 1
            except Exception as e:
                self.plugin_logger.error(f"发送每日总结到 {target} 失败: {e}")

        if success_count > 0:
            yield event.plain_result(
                f"✅ 已向 {success_count}/{len(targets)} 个目标发送昨日总结。"
            )
        else:
            yield event.plain_result("❌ 发送失败，所有目标均未发送成功。")

    @filter.command("weibo_get")
    async def weibo_get(self, event: AstrMessageEvent, url: str = ""):
        """抓取并推送指定链接的微博"""
        message_str: str = event.message_str or ""
        if message_str:
            parts = message_str.split(maxsplit=1)
            if len(parts) > 1:
                url = parts[1].strip()

        if not url:
            yield event.plain_result(
                "❌ 请提供微博链接。\n用法: /weibo_get <微博链接>\n\n支持格式:\n- https://weibo.com/uid/bid\n- https://m.weibo.cn/detail/bid\n- https://m.weibo.cn/status/bid"
            )
            return

        if "weibo.com" not in url and "weibo.cn" not in url:
            yield event.plain_result(
                "❌ 请提供正确的微博链接。\n支持域名: weibo.com 或 weibo.cn"
            )
            return

        parsed = self._parse_bid_from_url(url)
        if not parsed:
            yield event.plain_result(
                "❌ 无法从链接中解析微博 ID，请检查链接格式是否正确。\n\n支持格式:\n- https://weibo.com/uid/bid\n- https://m.weibo.cn/detail/bid\n- https://m.weibo.cn/status/bid"
            )
            return

        bid, uid = parsed
        if not bid:
            yield event.plain_result(
                "❌ 无法从链接中解析微博 ID，请确认链接包含有效的微博 bid。"
            )
            return

        yield event.plain_result("🔍 正在抓取微博内容...")

        post = await self._fetch_single_weibo(bid)
        if not post:
            yield event.plain_result(
                "❌ 无法获取该微博内容，可能原因:\n- 微博已被删除或设为私密\n- Cookie 已失效（请使用 /weibo_verify 检查）\n- 链接格式不正确"
            )
            return

        if not post.get("text") and not post.get("image_urls"):
            yield event.plain_result("❌ 该微博内容为空。")
            return

        targets = self.get_targets()
        if not targets:
            targets = [event.unified_msg_origin]

        msg_format = self.message_format
        actual_images = await self._send_post_to_targets(
            post, msg_format, targets, skip_log=True
        )

        image_info = f"，含 {actual_images} 张图片" if actual_images > 0 else ""
        video_info_text = ""
        if post.get("video_info"):
            video_info_text = (
                "，含视频" if post.get("_video_sent") else "，视频下载失败"
            )

        yield event.plain_result(
            f"✅ 已向 {len(targets)} 个目标推送 {post.get('username')} 的微博{image_info}{video_info_text}。"
        )

    @property
    def message_format(self) -> str:
        """获取并格式化消息模板"""
        return self._get_config("message_format", DEFAULT_MESSAGE_TEMPLATE).replace(
            "\\n", "\n"
        )

    async def _check_cookie_health(self) -> bool:
        """检查 Cookie 有效性"""
        try:
            resp = await self.client.get(
                f"{WEIBO_MOBILE_BASE}/api/config", headers=self.get_headers()
            )
            if resp.status_code == 200:
                data = resp.json()
                return bool((data.get("data") or {}).get("login"))
            return False
        except Exception as e:
            self.plugin_logger.debug(f"WeiboMonitor: 检查 Cookie 健康状态失败: {e}")
            return False

    def _set_cookie_health_status(self, status: str):
        """更新 Cookie 健康状态，供页面展示；状态保存失败不影响监控。"""
        if status not in {"valid", "invalid", "unknown", "unconfigured"}:
            status = "unknown"
        checked_at = self._get_utc8_now().strftime("%Y-%m-%d %H:%M:%S")
        changed = (
            getattr(self, "cookie_health_status", "unknown") != status
            or not getattr(self, "cookie_health_checked_at", "")
        )
        self.cookie_health_status = status
        self.cookie_health_checked_at = checked_at
        if changed and hasattr(self, "_data"):
            self._data["_cookie_health_status"] = status
            self._data["_cookie_health_checked_at"] = checked_at
            self._save_data()

    async def run_monitor(self):
        """后台监控主循环"""
        self.plugin_logger.info("微博监控任务已启动")
        await asyncio.sleep(10)

        last_check_time = 0
        error_backoff = 60
        last_cleanup_time = 0

        while self.running:
            try:
                now = self._get_utc8_now()
                current_time_str = now.strftime("%H:%M")
                current_date_str = now.strftime("%Y%m%d")

                retention = self._get_config("temp_media_retention_minutes", 10)
                if retention > 0:
                    cleanup_interval = max(60, retention * 60)
                    if (
                        asyncio.get_event_loop().time() - last_cleanup_time
                        >= cleanup_interval
                    ):
                        self._cleanup_temp_media()
                        last_cleanup_time = asyncio.get_event_loop().time()

                # 1. 检查是否需要发送每日总结
                summary_time = self._get_config("daily_summary_time", "08:00")
                if self._get_config("enable_daily_summary", False):
                    should_send_summary = False
                    if (
                        self.last_summary_date != current_date_str
                        and current_time_str >= summary_time
                    ):
                        should_send_summary = True
                    elif (
                        self.last_summary_date
                        and self.last_summary_date < current_date_str
                        and now.hour >= 8
                        and (int(now.strftime("%H%M")) - 800) < 10
                    ):
                        should_send_summary = True

                    if should_send_summary:
                        self.plugin_logger.info(
                            f"触发每日总结推送 (设定时间: {summary_time})"
                        )
                        try:
                            await self._send_daily_summary()
                        except Exception as e:
                            self.plugin_logger.error(f"发送每日总结失败: {e}")
                        self.last_summary_date = current_date_str
                        self._data["last_summary_date"] = current_date_str
                        self._save_data()

                # 1.5 检查是否需要推送热搜
                if self._get_config("enable_hotsearch", False):
                    hotsearch_interval = max(
                        5,
                        self._get_config(
                            "hotsearch_interval", DEFAULT_HOTSEARCH_INTERVAL
                        ),
                    )
                    if (
                        asyncio.get_event_loop().time() - self.last_hotsearch_time
                        >= hotsearch_interval * 60
                    ):
                        targets = self.get_delivery_targets("hotsearch")
                        if not targets:
                            self.plugin_logger.debug(
                                "WeiboMonitor: 未配置推送目标，跳过热搜推送"
                            )
                        else:
                            self.plugin_logger.info("开始获取微博热搜数据...")
                            try:
                                hot_items = await self._fetch_hotsearch()
                                if hot_items:
                                    await self._push_hotsearch(hot_items, targets)
                                else:
                                    self.plugin_logger.warning(
                                        "未获取到热搜数据，本次跳过"
                                    )
                            except Exception as e:
                                self.plugin_logger.error(f"热搜推送出错: {e}")
                        self.last_hotsearch_time = asyncio.get_event_loop().time()

                # 2. 检查是否需要执行监控
                base_interval = max(
                    1, self._get_config("check_interval", DEFAULT_CHECK_INTERVAL)
                )
                interval_jitter = self._get_config("check_interval_jitter", 0)
                actual_interval = max(
                    1,
                    random.randint(
                        base_interval - interval_jitter, base_interval + interval_jitter
                    ),
                )

                if (
                    asyncio.get_event_loop().time() - last_check_time
                    >= actual_interval * 60
                ):
                    urls = self._parse_urls(self._get_config("weibo_urls", []))
                    targets = self.get_targets()
                    msg_format = self.message_format
                    cookie = self._get_config("weibo_cookie", "")

                    if not cookie:
                        self.plugin_logger.warning(
                            "WeiboMonitor: 未配置微博Cookie，跳过本轮检查。请尽快配置！"
                        )
                    elif not urls:
                        self.plugin_logger.debug("WeiboMonitor: 未配置监控URL")
                    elif not self._get_all_subscribed_sessions():
                        self.plugin_logger.debug("WeiboMonitor: 未配置推送目标会话ID")
                    else:
                        # 检查 Cookie 健康
                        is_cookie_healthy = await self._check_cookie_health()
                        self._set_cookie_health_status(
                            "valid" if is_cookie_healthy else "invalid"
                        )
                        if not is_cookie_healthy:
                            if not self.cookie_invalid_notified:
                                self.plugin_logger.warning(
                                    "WeiboMonitor: 检测到 Cookie 已失效！已向用户发送通知。"
                                )
                                chain = MessageChain().message(
                                    "⚠️ 微博监控助手提醒：检测到您的微博 Cookie 已失效，插件将无法正常抓取数据。请尽快在后台更新 Cookie 以恢复监控功能！"
                                )

                                # 获取通知目标：优先使用专门配置的通知目标，否则使用默认推送目标
                                notification_target = self._get_config(
                                    "cookie_notification_target", ""
                                )
                                if (
                                    isinstance(notification_target, str)
                                    and notification_target.strip()
                                ):
                                    notify_targets = [
                                        t.strip()
                                        for t in notification_target.split(",")
                                        if t.strip()
                                    ]
                                elif (
                                    isinstance(notification_target, list)
                                    and notification_target
                                ):
                                    notify_targets = []
                                    for item in notification_target:
                                        item_str = str(item).strip()
                                        if "," in item_str:
                                            notify_targets.extend(
                                                [
                                                    t.strip()
                                                    for t in item_str.split(",")
                                                    if t.strip()
                                                ]
                                            )
                                        elif item_str:
                                            notify_targets.append(item_str)
                                    if not notify_targets:
                                        notify_targets = targets
                                else:
                                    notify_targets = list(
                                        set(targets)
                                        | self._get_all_subscribed_sessions()
                                    )

                                for target in notify_targets:
                                    try:
                                        await self.context.send_message(target, chain)
                                    except Exception:
                                        pass
                                self.cookie_invalid_notified = True
                            self.plugin_logger.debug(
                                "WeiboMonitor: Cookie 已失效，跳过本轮抓取。"
                            )
                        else:
                            if self.cookie_invalid_notified:
                                self.plugin_logger.info(
                                    "WeiboMonitor: 检测到 Cookie 已更新为有效状态。"
                                )
                                self.cookie_invalid_notified = False  # 恢复通知标志

                            self.plugin_logger.info(
                                f"开始新一轮监控检查，共 {len(urls)} 个账号"
                            )
                            base_req_interval = self._get_config(
                                "request_interval", DEFAULT_REQUEST_INTERVAL
                            )
                            req_jitter = self._get_config("request_interval_jitter", 0)

                            cycle_success = True
                            try:
                                await self._process_monitor_cycle(
                                    urls, base_req_interval, req_jitter, msg_format
                                )
                            except Exception as cycle_error:
                                self.plugin_logger.error(
                                    f"监控周期执行失败: {cycle_error}"
                                )
                                cycle_success = False

                            if cycle_success:
                                if self._consecutive_errors > 0:
                                    self._consecutive_errors = 0
                                    error_backoff = 60
                                    self.plugin_logger.info(
                                        "WeiboMonitor: 连续错误已清除，恢复正常监控频率"
                                    )
                                self.plugin_logger.info(
                                    f"本轮监控检查完成，下次检查将在约 {actual_interval} 分钟后"
                                )
                            else:
                                self._consecutive_errors += 1
                                error_backoff = min(
                                    self._max_error_backoff,
                                    60 * (2 ** min(self._consecutive_errors, 5)),
                                )
                                self.plugin_logger.warning(
                                    f"连续错误次数: {self._consecutive_errors}，退避等待: {error_backoff}秒"
                                )

                    last_check_time = asyncio.get_event_loop().time()
                    self.next_push_time = (
                        self._get_utc8_now()
                        + timedelta(minutes=actual_interval)
                    ).strftime("%Y-%m-%d %H:%M:%S")

                # 监控周期失败时按指数退避；成功后恢复默认 60 秒轮询。
                await asyncio.sleep(
                    error_backoff if self._consecutive_errors > 0 else 60
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._consecutive_errors += 1
                error_backoff = min(
                    self._max_error_backoff,
                    60 * (2 ** min(self._consecutive_errors, 5)),
                )
                self.plugin_logger.error(
                    f"WeiboMonitor 运行时错误 (连续错误 {self._consecutive_errors} 次): {e}"
                )
                import traceback

                self.plugin_logger.error(traceback.format_exc())
                self.plugin_logger.info(f"退避 {error_backoff} 秒后重试...")
                await asyncio.sleep(error_backoff)

    def _parse_urls(self, urls_raw: Any) -> List[str]:
        """解析监控URL列表，支持字符串逗号分隔或列表格式"""
        if isinstance(urls_raw, str):
            return [u.strip() for u in urls_raw.split(",") if u.strip()]

        # 兼容处理列表中包含逗号分隔字符串的情况
        urls = []
        if isinstance(urls_raw, list):
            for item in urls_raw:
                item_str = str(item).strip()
                if "," in item_str:
                    urls.extend([u.strip() for u in item_str.split(",") if u.strip()])
                elif item_str:
                    urls.append(item_str)
        return urls

    async def _process_monitor_cycle(
        self, urls: List[str], base_req_interval: int, req_jitter: int, msg_format: str
    ):
        for i, url in enumerate(urls):
            try:
                if i > 0:
                    actual_req_interval = max(
                        1,
                        random.randint(
                            base_req_interval - req_jitter,
                            base_req_interval + req_jitter,
                        ),
                    )
                    await asyncio.sleep(actual_req_interval)

                uid = await self.parse_uid(url)
                if not uid:
                    self.plugin_logger.warning(
                        f"WeiboMonitor: 无法解析URL {url}，已跳过"
                    )
                    continue

                new_posts = await self.check_weibo(uid)
                if new_posts:
                    uid_targets = self._get_targets_for_uid(uid)
                    if uid_targets:
                        for post in new_posts:
                            await self.push_queue.put((post, uid_targets, msg_format))
                        self.plugin_logger.info(
                            f"WeiboMonitor: UID {uid} 发现 {len(new_posts)} 条新微博，已加入推送队列"
                        )
                    else:
                        self.plugin_logger.debug(
                            f"WeiboMonitor: UID {uid} 没有可推送的目标会话"
                        )
            except Exception as e:
                self.plugin_logger.error(f"WeiboMonitor: 检查URL {url} 时出错: {e}")

    async def _push_consumer(self):
        """后台推送消费者：持续从队列取出待推送微博，逐条发送。
        与监控周期解耦，避免大文件下载阻塞检查节奏。
        """
        while True:
            queue_item = None
            try:
                queue_item = await self.push_queue.get()
                post, targets, msg_format = queue_item
                self.plugin_logger.info(
                    f"[推送队列] 开始推送 {post.get('username')} 的微博，队列剩余 {self.push_queue.qsize()}"
                )
                await self._send_post_to_targets(post, msg_format, targets)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.plugin_logger.error(f"[推送队列] 推送出错: {e}")
            finally:
                if queue_item is not None:
                    self.push_queue.task_done()

    async def _send_new_posts(
        self,
        new_posts: List[dict],
        targets: List[str],
        msg_format: str,
        fallback_target: str = None,
        skip_log: bool = False,
    ):
        """发送新微博到指定目标（文本与图片分别独立发送，兼容所有平台）"""
        if not targets and fallback_target:
            targets = [fallback_target]

        if not targets:
            self.plugin_logger.debug("WeiboMonitor: 没有配置推送目标，跳过推送")
            return

        for post in new_posts:
            await self._send_post_to_targets(post, msg_format, targets, skip_log)
            self.plugin_logger.info(
                f"WeiboMonitor: 已推送 {post.get('username')} 的更新到 {len(targets)} 个目标"
            )

    async def parse_uid(self, url: str) -> Optional[str]:
        """
        解析微博URL或用户名，提取UID。
        支持:
        1. 直接输入UID (如: 12345678)
        2. 个人主页URL (如: https://m.weibo.cn/u/12345678)
        3. 微博域名URL (如: https://weibo.com/u/12345678)
        4. 用户名跳转URL (如: https://weibo.com/n/用户名)
        """
        url = url.strip()
        if url.isdigit():
            return url

        match = re.search(r"weibo\.(com|cn)/u/(\d+)", url)
        if match:
            return match.group(2)

        match_name = re.search(r"weibo\.(com|cn)/n/([^/?#]+)", url)
        if match_name:
            name = match_name.group(2)
            try:
                async with self._request_semaphore:
                    resp = await self.client.get(
                        f"{WEIBO_MOBILE_BASE}/n/{name}",
                        headers=self.get_headers(),
                    )
                if resp.status_code == 429:
                    self.plugin_logger.warning(
                        "WeiboMonitor: 解析用户名时触发限流 (429)，等待后重试"
                    )
                    await asyncio.sleep(60)
                    async with self._request_semaphore:
                        resp = await self.client.get(
                            f"{WEIBO_MOBILE_BASE}/n/{name}",
                            headers=self.get_headers(),
                        )
                final_url = str(resp.url)
                match_uid = re.search(r"/u/(\d+)", final_url)
                if match_uid:
                    return match_uid.group(1)
                self.plugin_logger.debug(
                    f"WeiboMonitor: 用户名 {name} 跳转后无法解析UID，最终URL: {final_url}"
                )
            except Exception as e:
                self.plugin_logger.error(f"WeiboMonitor: 解析用户名 {name} 失败: {e}")
        return None

    async def _fetch_weibo_cards(self, uid: str) -> List[dict]:
        """获取指定UID的微博卡片列表"""
        api_url = f"{WEIBO_API_BASE}?type=uid&value={uid}&containerid=107603{uid}"
        try:
            async with self._request_semaphore:
                resp = await self.client.get(api_url, headers=self.get_headers(uid))
            if resp.status_code == 429:
                self.plugin_logger.warning(
                    f"WeiboMonitor: 触发限流 (429)，UID: {uid}，等待 60 秒后重试"
                )
                await asyncio.sleep(60)
                async with self._request_semaphore:
                    resp = await self.client.get(api_url, headers=self.get_headers(uid))
            if resp.status_code != 200:
                self.plugin_logger.error(
                    f"WeiboMonitor: 接口请求失败 (状态码 {resp.status_code}), UID: {uid}"
                )
                return []
            try:
                data = resp.json()
            except ValueError as e:
                self.plugin_logger.error(
                    f"WeiboMonitor: 解析接口返回的JSON数据失败, UID: {uid}, 错误: {e}"
                )
                return []
            if data.get("ok") != 1:
                self.plugin_logger.debug(
                    f"WeiboMonitor: 接口返回数据状态异常, UID: {uid}"
                )
                return []
            return (data.get("data") or {}).get("cards", [])
        except Exception as e:
            self.plugin_logger.error(f"WeiboMonitor: 获取UID {uid} 数据时出错: {e}")
            return []

    def _extract_valid_mblogs(
        self, cards: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], str]:
        """从卡片列表中提取有效的微博博文，并过滤置顶"""
        valid_mblogs: List[Dict[str, Any]] = []
        username = "未知用户"

        for card in cards:
            if not isinstance(card, dict):
                continue
            if card.get("card_type") == 9 and isinstance(card.get("mblog"), dict):
                mblog = card["mblog"]
                # 严格的置顶过滤
                is_top = any(
                    [
                        mblog.get("isTop"),
                        mblog.get("is_top"),
                        card.get("is_top"),
                        mblog.get("top"),
                        (mblog.get("title") or {}).get("text") == "置顶",
                    ]
                )
                if is_top:
                    continue

                valid_mblogs.append(mblog)
                if username == "未知用户":
                    username = (mblog.get("user") or {}).get("screen_name", "未知用户")

        return valid_mblogs, username

    async def check_weibo(
        self, uid: str, force_fetch: bool = False
    ) -> List[Dict[str, Any]]:
        """
        检查指定UID的最新微博。
        :param uid: 微博用户ID
        :param force_fetch: 是否强制获取最新一条（不比较last_id）
        :return: 包含新微博信息的列表
        """
        try:
            self.plugin_logger.debug(f"正在检查 UID: {uid}")
            cards = await self._fetch_weibo_cards(uid)
            if not cards:
                self.plugin_logger.debug(f"UID {uid} 未获取到卡片数据")
                return []

            # 复用微博列表响应中的用户资料，不增加额外请求或 Cookie 压力。
            for card in cards:
                mblog = card.get("mblog") if isinstance(card, dict) else None
                user = mblog.get("user") if isinstance(mblog, dict) else None
                if isinstance(user, dict):
                    self._update_account_profile(uid, user)
                    break

            valid_mblogs, username = self._extract_valid_mblogs(cards)
            if not valid_mblogs:
                self.plugin_logger.debug(f"UID {uid} ({username}) 未发现有效的微博博文")
                return []

            self.plugin_logger.debug(
                f"UID {uid} ({username}) 获取到 {len(valid_mblogs)} 条有效博文"
            )

            last_id_key = f"last_id_{uid}"
            last_id_str = await self.get_kv_data(last_id_key, "0")
            last_id = int(last_id_str)

            # 初始化检查：全新监控或会话首次检查
            if not force_fetch and (
                last_id == 0 or uid not in self.session_initialized_uids
            ):
                return await self._initialize_monitor(
                    uid, username, valid_mblogs, last_id_key, last_id
                )

            self.session_initialized_uids.add(uid)

            # 收集新微博
            new_posts = self._collect_new_posts(
                uid, valid_mblogs, last_id, force_fetch, username
            )

            # 更新最新ID
            if not force_fetch:
                await self._update_last_id(valid_mblogs, last_id, last_id_key)

            if new_posts:
                self.plugin_logger.info(
                    f"UID {uid} ({username}) 发现 {len(new_posts)} 条新微博"
                )
                new_posts.reverse()  # 按时间从旧到新排列
            else:
                self.plugin_logger.debug(
                    f"UID {uid} ({username}) 没有新微博 (last_id: {last_id})"
                )

            return new_posts
        except Exception as e:
            self.plugin_logger.error(f"WeiboMonitor: 检查UID {uid} 时出错: {e}")
            return []

    async def _initialize_monitor(
        self,
        uid: str,
        username: str,
        valid_mblogs: List[Dict[str, Any]],
        last_id_key: str,
        old_last_id: int,
    ) -> List[Dict[str, Any]]:
        """初始化监控状态，记录起始ID"""
        latest_id_val = valid_mblogs[0].get("id")
        if latest_id_val:
            latest_id = int(latest_id_val)
            await self.put_kv_data(last_id_key, str(latest_id))
            self.session_initialized_uids.add(uid)

            if old_last_id == 0:
                self.plugin_logger.info(
                    f"WeiboMonitor: 已初始化全新监控 UID {uid} ({username})，起始 ID: {latest_id}"
                )

                # 如果开启了每日日志，将获取到的历史微博记录下来
                if self._get_config("enable_daily_log", False):
                    self.plugin_logger.info(
                        f"WeiboMonitor: 正在将 UID {uid} 的历史微博记录到日志..."
                    )
                    for mblog in reversed(valid_mblogs):  # 从旧到新记录
                        text = self.clean_text(mblog.get("text", ""))
                        bid = mblog.get("bid")
                        if not bid:
                            continue
                        link = f"{WEIBO_WEB_BASE}/{uid}/{bid}"
                        created_at_raw = mblog.get("created_at")
                        created_at = self._parse_weibo_time(created_at_raw)

                        post = {
                            "text": text,
                            "link": link,
                            "username": username,
                            "created_at": created_at,
                            "image_urls": self._extract_image_urls(mblog),
                            "video_info": self._extract_video_info(mblog),
                        }
                        self._log_to_daily_file(post, record_type="initial_snapshot")
            else:
                self.plugin_logger.info(
                    f"WeiboMonitor: 已同步会话初始状态，UID {uid} ({username})，当前最新 ID: {latest_id}"
                )
        return []

    def _collect_new_posts(
        self,
        uid: str,
        valid_mblogs: List[Dict[str, Any]],
        last_id: int,
        force_fetch: bool,
        username: str,
    ) -> List[Dict[str, Any]]:
        """收集新的微博帖子，应用屏蔽词过滤、原创/转发过滤"""
        new_posts: List[Dict[str, Any]] = []
        filter_keywords = self._get_config("filter_keywords", [])
        send_original = self._get_config("send_original", True)
        send_forward = self._get_config("send_forward", True)

        for mblog in valid_mblogs:
            current_id_val = mblog.get("id")
            if not current_id_val:
                continue

            current_id = int(current_id_val)

            # 停止条件：检查到旧帖
            if not force_fetch and current_id <= last_id:
                break

            # 区分原创和转发
            is_forward = "retweeted_status" in mblog
            if is_forward and not send_forward:
                self.plugin_logger.info(
                    f"WeiboMonitor: 微博 {current_id} 是转发微博，已根据配置跳过推送"
                )
                continue
            if not is_forward and not send_original:
                self.plugin_logger.info(
                    f"WeiboMonitor: 微博 {current_id} 是原创微博，已根据配置跳过推送"
                )
                continue

            text = self.clean_text(mblog.get("text", ""))

            # 屏蔽词过滤（黑名单）
            if self._has_filter_keyword(text, filter_keywords, current_id):
                continue

            # 白名单关键词过滤（只有包含白名单关键词才推送）
            whitelist_keywords = self._get_config("whitelist_keywords", [])
            if self._should_skip_by_whitelist(text, whitelist_keywords, current_id):
                continue

            bid = mblog.get("bid")
            if not bid:
                self.plugin_logger.debug(
                    f"WeiboMonitor: 微博 {current_id} 缺少bid字段，已跳过"
                )
                continue
            link = f"{WEIBO_WEB_BASE}/{uid}/{bid}"

            created_at_raw = mblog.get("created_at")
            created_at = self._parse_weibo_time(created_at_raw)
            image_urls = self._extract_image_urls(mblog)
            video_info = self._extract_video_info(mblog)

            new_posts.append(
                {
                    "text": text,
                    "link": link,
                    "username": username,
                    "created_at": created_at,
                    "image_urls": image_urls,
                    "video_info": video_info,
                }
            )

            if force_fetch:
                break

        return new_posts

    def _has_filter_keyword(
        self, text: str, filter_keywords: List[str], post_id: int
    ) -> bool:
        """检查文本是否包含屏蔽词"""
        for keyword in filter_keywords:
            if keyword and keyword in text:
                self.plugin_logger.info(
                    f"WeiboMonitor: 微博 {post_id} 包含屏蔽词 '{keyword}'，已跳过推送"
                )
                return True
        return False

    def _should_skip_by_whitelist(
        self, text: str, whitelist_keywords: List[str], post_id: int
    ) -> bool:
        """检查文本是否应该被白名单过滤跳过（只有包含白名单关键词才允许推送）"""
        if not whitelist_keywords:
            return False
        for keyword in whitelist_keywords:
            if keyword and keyword in text:
                self.plugin_logger.info(
                    f"WeiboMonitor: 微博 {post_id} 包含白名单关键词 '{keyword}'，允许推送"
                )
                return False
        self.plugin_logger.info(
            f"WeiboMonitor: 微博 {post_id} 不包含任何白名单关键词，已跳过推送"
        )
        return True

    async def _update_last_id(
        self, valid_mblogs: List[Dict[str, Any]], last_id: int, last_id_key: str
    ):
        """更新记录的最新微博ID"""
        latest_id_val = valid_mblogs[0].get("id")
        if latest_id_val:
            latest_id = int(latest_id_val)
            if latest_id > last_id:
                await self.put_kv_data(last_id_key, str(latest_id))

    def clean_text(self, text: str) -> str:
        """清理微博正文中的HTML标签并处理换行"""
        if not text:
            return ""
        if not isinstance(text, str):
            return str(text)

        try:
            # 移除"全文"链接
            text = re.sub(r"<a[^>]*>全文</a>", "", text)

            soup = BeautifulSoup(text, "html.parser")

            # 处理图片：将alt文本替换为emoji
            for img in soup.find_all("img"):
                alt = img.get("alt", "")
                if alt:
                    img.replace_with(alt)

            # 处理超链接：移除所有超链接格式，仅保留链接内的文本内容，提升阅读观感
            for a in soup.find_all("a"):
                link_text = a.get_text()
                a.replace_with(link_text)

            # 将 <br> 标签替换为换行符
            for br in soup.find_all("br"):
                br.replace_with("\n")

            # 获取纯文本
            text = soup.get_text()

            # 清理多余的空白字符
            text = re.sub(r"\n\s+", "\n", text)
            text = re.sub(r"\s+\n", "\n", text)
            text = re.sub(r"\n{3,}", "\n\n", text)

            return text.strip()
        except Exception as e:
            self.plugin_logger.error(f"WeiboMonitor: 清理文本内容失败: {e}")
            return text
