import asyncio
import re
import httpx
import os
import json
import base64
import random
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple, Dict, Any
from functools import wraps
from urllib.parse import quote
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from bs4 import BeautifulSoup

# 常量定义
DEFAULT_CHECK_INTERVAL = 10  # 默认检查间隔（分钟）
DEFAULT_REQUEST_INTERVAL = 5  # 默认请求间隔（秒）
DEFAULT_TIMEOUT = 20  # 默认HTTP请求超时（秒）
MAX_CONCURRENT_REQUESTS = 5  # 最大并发请求数
DEFAULT_MESSAGE_TEMPLATE = "🔔 {name} 发微博啦！\n\n{weibo}\n\n链接: {link}"
WEIBO_API_BASE = "https://m.weibo.cn/api/container/getIndex"
WEIBO_MOBILE_BASE = "https://m.weibo.cn"
WEIBO_WEB_BASE = "https://weibo.com"
HOTSEARCH_API_URL = "https://weibo.com/ajax/side/hotSearch"
DEFAULT_HOTSEARCH_INTERVAL = 60
DEFAULT_HOTSEARCH_TOP_N = 10
DEFAULT_HOTSEARCH_TEMPLATE = "🔥 微博热搜榜 Top {top_n}\n⏰ 更新时间: {time}\n\n{items}"


@register("astrbot_plugin_weibo_monitor", "Sayaka", "定时监控微博用户动态并推送到指定会话。", "v1.14.0", "https://github.com/jiantoucn/astrbot_plugin_weibo_monitor")
class WeiboMonitor(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.monitor_task: Optional[asyncio.Task] = None
        self.cookie_invalid_notified = False # cookie 失效是否已通知
        
        # 确保数据目录存在
        self.data_dir = StarTools.get_data_dir()
        if not self.data_dir.exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_file = self.data_dir / "monitor_data.json"
        self.logs_dir = self.data_dir / "logs"
        if not self.logs_dir.exists():
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            
        # 初始化日志
        self.plugin_logger = logging.getLogger("astrbot_plugin_weibo_monitor")
        self.plugin_logger.setLevel(logging.DEBUG)
        self.plugin_logger.propagate = False # 不向上冒泡到 root logger
        self.setup_logging()
        
        # 检查Cookie是否配置
        cookie = self.config.get("weibo_cookie", "")
        if not cookie:
            self.plugin_logger.warning("WeiboMonitor: 未配置微博Cookie，插件无法正常工作！请在插件设置中填写weibo_cookie。")
        
        # 配置HTTP客户端，添加重试、超时和连接池设置
        self.limits = httpx.Limits(
            max_keepalive_connections=10,
            max_connections=MAX_CONCURRENT_REQUESTS,
            keepalive_expiry=30.0
        )
        transport = httpx.AsyncHTTPTransport(retries=2)
        self.client = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            transport=transport,
            follow_redirects=True,
            limits=self.limits
        )
        self.running = True
        self.session_initialized_uids: set[str] = set()
        self.last_summary_date: str = ""
        self._request_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        self._consecutive_errors = 0
        self._max_error_backoff = 300  # 最大退避时间5分钟

        # 兼容旧路径迁移 (data/astrbot_plugin_weibo_monitor -> StarTools.get_data_dir())
        old_data_file = os.path.join("data", "astrbot_plugin_weibo_monitor", "monitor_data.json")
        if not self.data_file.exists() and os.path.exists(old_data_file):
            try:
                import shutil
                shutil.copy2(old_data_file, self.data_file)
                self.plugin_logger.info(f"WeiboMonitor: 已从旧路径迁移数据到 {self.data_file}")
            except Exception as e:
                self.plugin_logger.error(f"WeiboMonitor: 迁移数据失败: {e}")

        self._data = self._load_data()
        
        self.last_summary_date = self._data.get("last_summary_date", "")
        self.last_hotsearch_time = 0
        self._init_last_hotsearch_time()

        self._similarity_cache: Dict[str, List[Tuple[str, str]]] = {}
        self._load_similarity_cache()

        # 启动后台监控任务
        self.monitor_task = asyncio.create_task(self.run_monitor())

    def setup_logging(self):
        """设置运行日志"""
        existing_handlers = self.plugin_logger.handlers
        if any(isinstance(h, logging.FileHandler) for h in existing_handlers):
            return
            
        for handler in existing_handlers:
            if not isinstance(handler, logging.FileHandler):
                self.plugin_logger.removeHandler(handler)
            
        if self.config.get("enable_plugin_log", False):
            log_file = self.data_dir / "plugin.log"
            max_size_mb = self.config.get("plugin_log_max_size", 1)
            file_handler = RotatingFileHandler(
                log_file, 
                maxBytes=max_size_mb * 1024 * 1024, 
                backupCount=3, 
                encoding="utf-8"
            )
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            file_handler.setFormatter(formatter)
            self.plugin_logger.addHandler(file_handler)
            self.plugin_logger.info("运行日志功能已启用")
        
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
        if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in self.plugin_logger.handlers):
            self.plugin_logger.addHandler(console_handler)

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
                if len(parts) == 2: # MM-DD
                    return f"{now.year}-{time_str} 00:00:00"
                elif len(parts) == 3: # YYYY-MM-DD
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

    def _log_to_daily_file(self, post: dict, skip_log: bool = False):
        """记录每日推送记录 (JSON 格式)"""
        if skip_log or not self.config.get("enable_daily_log", False):
            return
            
        now = self._get_utc8_now()
        publish_time_str = post.get("created_at")
        
        # 确定日志文件名和时间戳
        if publish_time_str:
            try:
                # 解析 YYYY-MM-DD HH:mm:ss
                publish_time = datetime.strptime(publish_time_str, "%Y-%m-%d %H:%M:%S")
                date_str = publish_time.strftime("%Y%m%d")
                log_time_str = publish_time_str
            except:
                date_str = now.strftime("%Y%m%d")
                log_time_str = now.strftime("%Y-%m-%d %H:%M:%S")
        else:
            date_str = now.strftime("%Y%m%d")
            log_time_str = now.strftime("%Y-%m-%d %H:%M:%S")

        log_file = self.logs_dir / f"{date_str}.log"
        
        log_entry = {
            "time": log_time_str,
            "username": post.get("username", "未知用户"),
            "content": post.get("text", ""),
            "link": post.get("link", "")
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
                        except:
                            continue
            
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        except Exception as e:
            self.plugin_logger.error(f"记录每日日志失败: {e}")

    def _log_hotsearch_to_daily(self, items: List[dict]):
        """记录热搜推送到每日日志 (JSON 格式)"""
        if not self.config.get("enable_daily_log", False):
            return

        now = self._get_utc8_now()
        date_str = now.strftime("%Y%m%d")
        log_file = self.logs_dir / f"{date_str}.log"

        log_entry = {
            "type": "hotsearch",
            "time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(items),
            "items": [item.get("desc", "") for item in items]
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
                                    if entry_time > cutoff and (last_push_time is None or entry_time > last_push_time):
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
                    if stored_time > cutoff and (last_push_time is None or stored_time > last_push_time):
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
        if not self.config.get("enable_daily_summary", False):
            return

        now = self._get_utc8_now()
        yesterday = now - timedelta(days=1)
        date_str = yesterday.strftime("%Y%m%d")
        log_file = self.logs_dir / f"{date_str}.log"

        if not log_file.exists():
            self.plugin_logger.info(f"未找到昨日 ({date_str}) 的日志文件，跳过每日总结。")
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
                    except:
                        continue
        except Exception as e:
            self.plugin_logger.error(f"读取昨日日志文件失败: {e}")
            return

        if not has_any_entry:
            summary_msg = f"📊 微博监控昨日 ({yesterday.strftime('%Y-%m-%d')}) 总结：\n\n昨日未推送任何动态。"
        else:
            summary_lines = [f"📊 微博监控昨日 ({yesterday.strftime('%Y-%m-%d')}) 总结：\n"]
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

        targets = self.get_targets()
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
            async with self._request_semaphore:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "application/json, text/plain, */*",
                    "Referer": "https://weibo.com/",
                }
                
                # 尝试不带 Cookie 获取
                resp = await self.client.get(HOTSEARCH_API_URL, headers=headers)
                
                if resp.status_code == 429:
                    self.plugin_logger.warning("获取热搜数据触发限流 (429)，等待 60 秒后重试")
                    await asyncio.sleep(60)
                    resp = await self.client.get(HOTSEARCH_API_URL, headers=headers)
                
                need_cookie_fallback = False
                data = {}
                if resp.status_code != 200:
                    self.plugin_logger.warning(f"无Cookie获取热搜失败，状态码: {resp.status_code}")
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
                    cookie = self.config.get("weibo_cookie", "")
                    if not cookie:
                        self.plugin_logger.error("无Cookie获取失败，且未配置 weibo_cookie，无法兜底")
                        return []
                    
                    self.plugin_logger.info("尝试携带 Cookie 获取热搜数据兜底...")
                    headers["Cookie"] = cookie
                    resp = await self.client.get(HOTSEARCH_API_URL, headers=headers)
                    
                    if resp.status_code != 200:
                        self.plugin_logger.error(f"带Cookie获取热搜数据失败，状态码: {resp.status_code}")
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

                filter_ads = self.config.get("hotsearch_filter_ads", True)
                items = []
                for item in realtime:
                    if not isinstance(item, dict):
                        continue
                    if filter_ads and (item.get("is_ad") == 1 or item.get("is_ad_pos") == 1):
                        self.plugin_logger.debug(f"已过滤广告位热搜: {item.get('word', '')}")
                        continue
                    
                    word = item.get("word") or item.get("note")
                    if not word:
                        continue

                    heat = str(item.get("num", ""))
                    
                    items.append({
                        "desc": str(word),
                        "heat": heat,
                        "scheme": f"https://s.weibo.com/weibo?q={quote(word)}"
                    })

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

        top_n = self.config.get("hotsearch_top_n", DEFAULT_HOTSEARCH_TOP_N)
        display_items = items[:top_n]

        now = self._get_utc8_now()
        time_str = now.strftime("%Y-%m-%d %H:%M")

        item_lines = []
        for idx, item in enumerate(display_items, 1):
            item_lines.append(f"{idx}. {item['desc']}\n   {item['scheme']}")

        items_text = "\n\n".join(item_lines)

        template = self.config.get(
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
            self.plugin_logger.info(f"已向 {sent_count}/{len(targets)} 个目标推送热搜榜")
            self._log_hotsearch_to_daily(display_items)
            self._data["last_hotsearch_push_time"] = now.strftime("%Y-%m-%d %H:%M:%S")
            self._save_data()

    def _load_data(self) -> dict:
        """从文件加载持久化数据，损坏时自动备份"""
        if self.data_file.exists():
            try:
                return json.loads(self.data_file.read_text(encoding="utf-8"))
            except Exception as e:
                self.plugin_logger.error(f"WeiboMonitor: 加载数据文件失败: {e}")
                # 自动备份损坏的文件
                try:
                    backup_file = self.data_file.with_suffix(f".bak.{int(asyncio.get_event_loop().time())}")
                    self.data_file.rename(backup_file)
                    self.plugin_logger.info(f"WeiboMonitor: 已将损坏的数据文件备份为 {backup_file}")
                except Exception as backup_err:
                    self.plugin_logger.error(f"WeiboMonitor: 备份损坏的数据文件失败: {backup_err}")
        return {}

    def _save_data(self):
        """将持久化数据保存到文件（原子写入，避免数据损坏）"""
        try:
            # 先写入临时文件，成功后再替换原文件，防止写入中断导致数据损坏
            temp_file = self.data_file.with_suffix(".tmp")
            temp_file.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=4), 
                encoding="utf-8"
            )
            # 原子替换
            temp_file.replace(self.data_file)
        except Exception as e:
            self.plugin_logger.error(f"WeiboMonitor: 保存数据文件失败: {e}")
            # 清理临时文件
            try:
                if temp_file.exists():
                    temp_file.unlink()
            except:
                pass

    def _load_similarity_cache(self):
        cache_file = self.data_dir / "similarity_cache.json"
        if not cache_file.exists():
            return
        try:
            raw = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for uid, entries in raw.items():
                    if isinstance(entries, list):
                        self._similarity_cache[uid] = [
                            (e["hash"], e["text"]) for e in entries if isinstance(e, dict) and "hash" in e and "text" in e
                        ]
                self.plugin_logger.debug(f"WeiboMonitor: 已加载相似度缓存，共 {sum(len(v) for v in self._similarity_cache.values())} 条")
        except Exception as e:
            self.plugin_logger.warning(f"WeiboMonitor: 加载相似度缓存失败: {e}")

    def _save_similarity_cache(self):
        cache_file = self.data_dir / "similarity_cache.json"
        try:
            serializable = {}
            for uid, entries in self._similarity_cache.items():
                serializable[uid] = [{"hash": h, "text": t} for h, t in entries]
            temp_file = cache_file.with_suffix(".tmp")
            temp_file.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_file.replace(cache_file)
        except Exception as e:
            self.plugin_logger.error(f"WeiboMonitor: 保存相似度缓存失败: {e}")
            try:
                if temp_file.exists():
                    temp_file.unlink()
            except:
                pass

    @staticmethod
    def _compute_simhash(text: str, bits: int = 64) -> int:
        tokens = list(text)
        if not tokens:
            return 0
        v = [0] * bits
        for token in tokens:
            token_hash = hash(token) & ((1 << bits) - 1)
            for i in range(bits):
                if (token_hash >> i) & 1:
                    v[i] += 1
                else:
                    v[i] -= 1
        fingerprint = 0
        for i in range(bits):
            if v[i] > 0:
                fingerprint |= (1 << i)
        return fingerprint

    @staticmethod
    def _hamming_distance(hash1: int, hash2: int) -> int:
        diff = hash1 ^ hash2
        count = 0
        while diff:
            count += 1
            diff &= diff - 1
        return count

    @staticmethod
    def _jaccard_similarity(text1: str, text2: str) -> float:
        set1 = set(text1)
        set2 = set(text2)
        if not set1 and not set2:
            return 1.0
        if not set1 or not set2:
            return 0.0
        intersection = len(set1 & set2)
        union = len(set1 | set2)
        return intersection / union if union > 0 else 0.0

    def _is_duplicate(self, uid: str, new_text: str) -> bool:
        history = self._similarity_cache.get(uid, [])
        if not history:
            return False
        cache_size = self.config.get("similarity_cache_size", 20)
        threshold = self.config.get("similarity_threshold", 0.7)
        new_hash = self._compute_simhash(new_text)
        for cached_hash, cached_text in history[:cache_size]:
            hamming = self._hamming_distance(new_hash, cached_hash)
            if hamming <= 3:
                similarity = self._jaccard_similarity(new_text, cached_text)
                if similarity >= threshold:
                    self.plugin_logger.info(
                        f"WeiboMonitor: 检测到相似微博（相似度: {similarity:.2%}，汉明距离: {hamming}），已跳过"
                    )
                    return True
        return False

    def _update_similarity_cache(self, uid: str, text: str):
        cache_size = self.config.get("similarity_cache_size", 20)
        text_hash = self._compute_simhash(text)
        if uid not in self._similarity_cache:
            self._similarity_cache[uid] = []
        self._similarity_cache[uid].insert(0, (text_hash, text))
        self._similarity_cache[uid] = self._similarity_cache[uid][:cache_size]
        self._save_similarity_cache()

    async def get_kv_data(self, key: str, default=None):
        """获取持久化键值对"""
        return self._data.get(key, default)

    async def put_kv_data(self, key: str, value):
        """设置并保存持久化键值对"""
        self._data[key] = value
        self._save_data()

    def get_headers(self, uid: str = "") -> Dict[str, str]:
        """获取请求头"""
        cookie = self.config.get("weibo_cookie", "")
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
        await self.client.aclose()
        self.plugin_logger.info("WeiboMonitor 插件已停止")

    def get_targets(self) -> List[str]:
        targets_raw = self.config.get("target_conversation_id", [])
        if isinstance(targets_raw, str):
            return [t.strip() for t in targets_raw.split(",") if t.strip()]
        
        # 兼容处理列表中包含逗号分隔字符串的情况
        targets = []
        if isinstance(targets_raw, list):
            for item in targets_raw:
                item_str = str(item).strip()
                if "," in item_str:
                    targets.extend([t.strip() for t in item_str.split(",") if t.strip()])
                elif item_str:
                    targets.append(item_str)
        return targets

    @filter.command("get_umo")
    async def get_umo(self, event: AstrMessageEvent):
        """获取当前会话的 ID (unified_msg_origin)，用于设置推送目标"""
        yield event.plain_result(
            f"当前会话 ID: {event.unified_msg_origin}\n请将此 ID 填入插件设置中的 target_conversation_id 项。"
        )

    @filter.command("weibo_export")
    async def weibo_export(self, event: AstrMessageEvent):
        """导出当前插件配置"""
        try:
            config_json = json.dumps(self.config, ensure_ascii=False)
            config_b64 = base64.b64encode(config_json.encode("utf-8")).decode("utf-8")
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
        if not config_str:
            yield event.plain_result("❌ 请提供配置字符串。用法: /weibo_import <配置字符串>")
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

            # 尝试重新设置日志（如果配置有变）
            self.setup_logging()

            # 尝试调用框架的配置保存接口（如果支持）
            try:
                if hasattr(self.context, "config_manager") and hasattr(self.context.config_manager, "save_config"):
                    self.context.config_manager.save_config()
            except:
                pass

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
        cookie = self.config.get("weibo_cookie", "")
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
                    yield event.plain_result(
                        "❌ Cookie 已失效或未登录（接口返回 login: false）。"
                    )
            else:
                yield event.plain_result(f"❌ 验证请求失败，状态码: {resp.status_code}")
        except Exception as e:
            self.plugin_logger.error(f"WeiboMonitor: 验证过程中出现错误: {e}")
            yield event.plain_result(f"❌ 验证过程中出现错误: {e}")

    @filter.command("weibo_cookie")
    async def weibo_cookie(self, event: AstrMessageEvent, cookie: str = ""):
        """更换微博 Cookie 并自动重载插件"""
        if not cookie:
            yield event.plain_result("❌ 请提供 Cookie。用法: /weibo_cookie <Cookie字符串>")
            return

        self.config["weibo_cookie"] = cookie
        self.cookie_invalid_notified = False

        try:
            if hasattr(self.context, "config_manager") and hasattr(self.context.config_manager, "save_config"):
                self.context.config_manager.save_config()
                saved = True
            else:
                saved = False
        except Exception as e:
            self.plugin_logger.error(f"WeiboMonitor: 保存配置失败: {e}")
            saved = False

        yield event.plain_result("🔄 Cookie 已更新，正在验证有效性...")
        try:
            resp = await self.client.get(
                "https://m.weibo.cn/api/config", headers=self.get_headers()
            )
            if resp.status_code == 200:
                data = resp.json()
                data_obj = data.get("data") or {}
                if data_obj.get("login"):
                    user = data_obj.get("user")
                    user_info = f"当前登录用户: {user.get('screen_name')} (UID: {user.get('id')})" if user else f"已登录 (UID: {data_obj.get('uid')})"
                    save_msg = "✅ 配置已持久化保存" if saved else "⚠️ 配置已更新但未能持久化保存，重启后可能丢失"
                    self.plugin_logger.info(f"WeiboMonitor: Cookie 已通过命令更换，{save_msg}")
                    yield event.plain_result(
                        f"✅ Cookie 更换成功！{user_info}\n{save_msg}\n"
                        f"🔄 正在重载插件..."
                    )
                    try:
                        if hasattr(self.context, "star_loader") and hasattr(self.context.star_loader, "reload"):
                            self.context.star_loader.reload("astrbot_plugin_weibo_monitor")
                        elif hasattr(self.context, "reload_plugin"):
                            self.context.reload_plugin("astrbot_plugin_weibo_monitor")
                        else:
                            yield event.plain_result("⚠️ 无法自动重载插件，请手动在 WebUI 插件管理中点击「重载插件」，或重启 AstrBot。\n💡 新 Cookie 已生效，无需重载亦可正常使用。")
                    except Exception as reload_err:
                        self.plugin_logger.warning(f"WeiboMonitor: 自动重载插件失败: {reload_err}")
                        yield event.plain_result("⚠️ 自动重载失败，请手动在 WebUI 插件管理中点击「重载插件」。\n💡 新 Cookie 已生效，无需重载亦可正常使用。")
                else:
                    yield event.plain_result(
                        "❌ Cookie 已更新但验证失败（接口返回 login: false），请检查 Cookie 是否正确。"
                    )
            else:
                yield event.plain_result(f"❌ Cookie 已更新但验证请求失败，状态码: {resp.status_code}")
        except Exception as e:
            self.plugin_logger.error(f"WeiboMonitor: 更换 Cookie 后验证出错: {e}")
            yield event.plain_result(f"❌ Cookie 已更新但验证过程出错: {e}")

    @filter.command("weibo_check")
    async def weibo_check(self, event: AstrMessageEvent):
        """立即检查并发送列表中第一个账号的最新微博信息 (仅限第一个)"""
        urls = self._parse_urls(self.config.get("weibo_urls", []))
        if not urls:
            yield event.plain_result("❌ 未在插件设置中配置监控URL。")
            return
            
        yield event.plain_result(f"🔍 正在检查首个微博账号的最新动态...")
        
        # 只取第一个 URL
        url = urls[0]
        targets = self.get_targets()
        msg_format = self.message_format
        
        uid = await self.parse_uid(url)
        if not uid:
            yield event.plain_result(f"❌ 无法解析URL: {url}")
            return

        latest_posts = await self.check_weibo(uid, force_fetch=True)
        if latest_posts:
            await self._send_new_posts(latest_posts, targets, msg_format, event.unified_msg_origin, skip_log=True)
            yield event.plain_result(f"✅ {latest_posts[0].get('username')} 已发送最新动态。")
        else:
            yield event.plain_result(f"ℹ️ UID {uid} 未获取到有效微博。")

    @filter.command("weibo_check_all")
    async def weibo_check_all(self, event: AstrMessageEvent):
        """立即检查并发送列表中所有账号的最新微博信息"""
        urls = self._parse_urls(self.config.get("weibo_urls", []))
        targets = self.get_targets()
        msg_format = self.message_format
        
        base_req_interval = self.config.get("request_interval", DEFAULT_REQUEST_INTERVAL)
        req_jitter = self.config.get("request_interval_jitter", 0)

        if not urls:
            yield event.plain_result("❌ 未在插件设置中配置监控URL。")
            return

        yield event.plain_result(
            f"🔍 正在立即检查 {len(urls)} 个微博账号的最新动态..."
        )

        results = []
        for i, url in enumerate(urls):
            if i > 0:
                actual_req_interval = max(1, random.randint(base_req_interval - req_jitter, base_req_interval + req_jitter))
                await asyncio.sleep(actual_req_interval)

            uid = await self.parse_uid(url)
            if not uid:
                results.append(f"❌ 无法解析URL: {url}")
                continue

            latest_posts = await self.check_weibo(uid, force_fetch=True)
            if latest_posts:
                await self._send_new_posts(latest_posts, targets, msg_format, event.unified_msg_origin, skip_log=True)
                results.append(f"✅ {latest_posts[0].get('username')} 已发送最新动态。")
            else:
                results.append(f"ℹ️ UID {uid} 未获取到有效微博。")

        yield event.plain_result("\n".join(results))

    @filter.command("weibo_hot")
    async def weibo_hot(self, event: AstrMessageEvent):
        """手动查询当前微博热搜榜"""
        if not self.config.get("enable_hotsearch", False):
            yield event.plain_result("❌ 热搜监控功能未开启，请先在插件设置中启用。")
            return

        targets = self.get_targets()
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
        """查看当前监控状态"""
        urls = self._parse_urls(self.config.get("weibo_urls", []))
        targets = self.get_targets()
        
        status_lines = ["📊 微博监控当前状态："]
        status_lines.append(f"- 监控账号数：{len(urls)} 个")
        status_lines.append(f"- 推送目标数：{len(targets)} 个")
        
        check_interval = self.config.get("check_interval", DEFAULT_CHECK_INTERVAL)
        status_lines.append(f"- 检查间隔：{check_interval} 分钟")
        
        cookie = self.config.get("weibo_cookie", "")
        cookie_status = "✅ 已配置" if cookie else "❌ 未配置"
        status_lines.append(f"- Cookie：{cookie_status}")
        
        status_lines.append(f"- 自动推送：{'✅ 开启' if targets and cookie else '❌ 关闭'}")
        
        daily_summary = self.config.get("enable_daily_summary", False)
        if daily_summary:
            summary_time = self.config.get("daily_summary_time", "08:00")
            status_lines.append(f"- 每日总结：✅ 开启 ({summary_time})")
        else:
            status_lines.append(f"- 每日总结：❌ 关闭")

        hotsearch_enabled = self.config.get("enable_hotsearch", False)
        if hotsearch_enabled:
            hotsearch_interval = self.config.get("hotsearch_interval", DEFAULT_HOTSEARCH_INTERVAL)
            hotsearch_top_n = self.config.get("hotsearch_top_n", DEFAULT_HOTSEARCH_TOP_N)
            status_lines.append(f"- 热搜监控：✅ 开启 (每 {hotsearch_interval} 分钟, Top {hotsearch_top_n})")
        else:
            status_lines.append(f"- 热搜监控：❌ 关闭")

        similarity_dedup = self.config.get("enable_similarity_dedup", False)
        if similarity_dedup:
            threshold = self.config.get("similarity_threshold", 0.7)
            cache_size = self.config.get("similarity_cache_size", 20)
            status_lines.append(f"- 相似度去重：🧪 实验中 (阈值: {threshold:.0%}, 缓存: {cache_size})")
        else:
            status_lines.append(f"- 相似度去重：❌ 关闭")

        if urls:
            status_lines.append(f"\n📋 监控列表：")
            for i, url in enumerate(urls[:5], 1):
                status_lines.append(f"  {i}. {url}")
            if len(urls) > 5:
                status_lines.append(f"  ... 等共 {len(urls)} 个")
        
        yield event.plain_result("\n".join(status_lines))

    @filter.command("weibo_summary")
    async def weibo_summary(self, event: AstrMessageEvent):
        """手动触发昨日总结推送"""
        if not self.config.get("enable_daily_summary", False):
            yield event.plain_result("❌ 每日总结功能未开启，请先在插件设置中启用。")
            return
        
        targets = self.get_targets()
        if not targets:
            yield event.plain_result("❌ 未配置推送目标，无法发送每日总结。")
            return
        
        yield event.plain_result("📊 正在生成昨日总结...")
        
        now = self._get_utc8_now()
        yesterday = now - timedelta(days=1)
        date_str = yesterday.strftime("%Y%m%d")
        log_file = self.logs_dir / f"{date_str}.log"
        
        if not log_file.exists():
            yield event.plain_result(f"ℹ️ 未找到昨日 ({yesterday.strftime('%Y-%m-%d')}) 的推送记录。")
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
                    except:
                        continue
        except Exception as e:
            self.plugin_logger.error(f"读取昨日日志文件失败: {e}")
            yield event.plain_result(f"❌ 读取日志失败: {e}")
            return
        
        if not has_any_entry:
            summary_msg = f"📊 微博监控昨日 ({yesterday.strftime('%Y-%m-%d')}) 总结：\n\n昨日未推送任何动态。"
        else:
            summary_lines = [f"📊 微博监控昨日 ({yesterday.strftime('%Y-%m-%d')}) 总结：\n"]
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
            yield event.plain_result(f"✅ 已向 {success_count}/{len(targets)} 个目标发送昨日总结。")
        else:
            yield event.plain_result("❌ 发送失败，所有目标均未发送成功。")

    @property
    def message_format(self) -> str:
        """获取并格式化消息模板"""
        return self.config.get(
            "message_format", DEFAULT_MESSAGE_TEMPLATE
        ).replace("\\n", "\n")

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

    async def run_monitor(self):
        """后台监控主循环"""
        self.plugin_logger.info("微博监控任务已启动")
        await asyncio.sleep(10)
        
        last_check_time = 0
        error_backoff = 60

        while self.running:
            try:
                now = self._get_utc8_now()
                current_time_str = now.strftime("%H:%M")
                current_date_str = now.strftime("%Y%m%d")
                
                # 重置错误计数和退避时间（正常运行时）
                if self._consecutive_errors > 0:
                    self._consecutive_errors = 0
                    error_backoff = 60
                    self.plugin_logger.info("WeiboMonitor: 连续错误已清除，恢复正常监控频率")
                
                # 1. 检查是否需要发送每日总结
                summary_time = self.config.get("daily_summary_time", "08:00")
                if self.config.get("enable_daily_summary", False):
                    should_send_summary = False
                    if self.last_summary_date != current_date_str and current_time_str >= summary_time:
                        should_send_summary = True
                    elif self.last_summary_date and self.last_summary_date < current_date_str and now.hour >= 8 and (int(now.strftime("%H%M")) - 800) < 10:
                        should_send_summary = True
                    
                    if should_send_summary:
                        self.plugin_logger.info(f"触发每日总结推送 (设定时间: {summary_time})")
                        try:
                            await self._send_daily_summary()
                        except Exception as e:
                            self.plugin_logger.error(f"发送每日总结失败: {e}")
                        self.last_summary_date = current_date_str
                        self._data["last_summary_date"] = current_date_str
                        self._save_data()

                # 1.5 检查是否需要推送热搜
                if self.config.get("enable_hotsearch", False):
                    hotsearch_interval = max(5, self.config.get("hotsearch_interval", DEFAULT_HOTSEARCH_INTERVAL))
                    if asyncio.get_event_loop().time() - self.last_hotsearch_time >= hotsearch_interval * 60:
                        targets = self.get_targets()
                        if not targets:
                            self.plugin_logger.debug("WeiboMonitor: 未配置推送目标，跳过热搜推送")
                        else:
                            self.plugin_logger.info("开始获取微博热搜数据...")
                            try:
                                hot_items = await self._fetch_hotsearch()
                                if hot_items:
                                    await self._push_hotsearch(hot_items, targets)
                                else:
                                    self.plugin_logger.warning("未获取到热搜数据，本次跳过")
                            except Exception as e:
                                self.plugin_logger.error(f"热搜推送出错: {e}")
                        self.last_hotsearch_time = asyncio.get_event_loop().time()

                # 2. 检查是否需要执行监控
                base_interval = max(1, self.config.get("check_interval", DEFAULT_CHECK_INTERVAL))
                interval_jitter = self.config.get("check_interval_jitter", 0)
                actual_interval = max(1, random.randint(base_interval - interval_jitter, base_interval + interval_jitter))
                
                if asyncio.get_event_loop().time() - last_check_time >= actual_interval * 60:
                    urls = self._parse_urls(self.config.get("weibo_urls", []))
                    targets = self.get_targets()
                    msg_format = self.message_format
                    cookie = self.config.get("weibo_cookie", "")
                    
                    if not cookie:
                        self.plugin_logger.warning("WeiboMonitor: 未配置微博Cookie，跳过本轮检查。请尽快配置！")
                    elif not urls:
                        self.plugin_logger.debug("WeiboMonitor: 未配置监控URL")
                    elif not targets:
                        self.plugin_logger.debug("WeiboMonitor: 未配置推送目标会话ID")
                    else:
                        # 检查 Cookie 健康
                        is_cookie_healthy = await self._check_cookie_health()
                        if not is_cookie_healthy:
                            if not self.cookie_invalid_notified:
                                self.plugin_logger.warning("WeiboMonitor: 检测到 Cookie 已失效！已向用户发送通知。")
                                chain = MessageChain().message("⚠️ 微博监控助手提醒：检测到您的微博 Cookie 已失效，插件将无法正常抓取数据。请尽快在后台更新 Cookie 以恢复监控功能！")
                                
                                # 获取通知目标：优先使用专门配置的通知目标，否则使用默认推送目标
                                notification_target = self.config.get("cookie_notification_target", "")
                                if isinstance(notification_target, str) and notification_target.strip():
                                    notify_targets = [t.strip() for t in notification_target.split(",") if t.strip()]
                                elif isinstance(notification_target, list) and notification_target:
                                    notify_targets = []
                                    for item in notification_target:
                                        item_str = str(item).strip()
                                        if "," in item_str:
                                            notify_targets.extend([t.strip() for t in item_str.split(",") if t.strip()])
                                        elif item_str:
                                            notify_targets.append(item_str)
                                    if not notify_targets:
                                        notify_targets = targets
                                else:
                                    notify_targets = targets
                                    
                                for target in notify_targets:
                                    try:
                                        await self.context.send_message(target, chain)
                                    except:
                                        pass
                                self.cookie_invalid_notified = True
                            self.plugin_logger.debug("WeiboMonitor: Cookie 已失效，跳过本轮抓取。")
                        else:
                            if self.cookie_invalid_notified:
                                self.plugin_logger.info("WeiboMonitor: 检测到 Cookie 已更新为有效状态。")
                                self.cookie_invalid_notified = False # 恢复通知标志

                            self.plugin_logger.info(f"开始新一轮监控检查，共 {len(urls)} 个账号")
                            base_req_interval = self.config.get("request_interval", DEFAULT_REQUEST_INTERVAL)
                            req_jitter = self.config.get("request_interval_jitter", 0)
                            
                            cycle_success = True
                            try:
                                await self._process_monitor_cycle(urls, base_req_interval, req_jitter, targets, msg_format)
                            except Exception as cycle_error:
                                self.plugin_logger.error(f"监控周期执行失败: {cycle_error}")
                                cycle_success = False
                            
                            if cycle_success:
                                self.plugin_logger.info(f"本轮监控检查完成，下次检查将在约 {actual_interval} 分钟后")
                            else:
                                self._consecutive_errors += 1
                                error_backoff = min(self._max_error_backoff, 60 * (2 ** min(self._consecutive_errors, 5)))
                                self.plugin_logger.warning(f"连续错误次数: {self._consecutive_errors}，退避等待: {error_backoff}秒")
                    
                    last_check_time = asyncio.get_event_loop().time()

                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._consecutive_errors += 1
                error_backoff = min(self._max_error_backoff, 60 * (2 ** min(self._consecutive_errors, 5)))
                self.plugin_logger.error(f"WeiboMonitor 运行时错误 (连续错误 {self._consecutive_errors} 次): {e}")
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

    async def _process_monitor_cycle(self, urls: List[str], base_req_interval: int, req_jitter: int, 
                                   targets: List[str], msg_format: str):
        """处理单个监控周期的所有URL检查"""
        for i, url in enumerate(urls):
            try:
                if i > 0:
                    actual_req_interval = max(1, random.randint(base_req_interval - req_jitter, base_req_interval + req_jitter))
                    await asyncio.sleep(actual_req_interval)

                uid = await self.parse_uid(url)
                if not uid:
                    self.plugin_logger.warning(f"WeiboMonitor: 无法解析URL {url}，已跳过")
                    continue

                new_posts = await self.check_weibo(uid)
                if new_posts:
                    await self._send_new_posts(new_posts, targets, msg_format)
            except Exception as e:
                self.plugin_logger.error(f"WeiboMonitor: 检查URL {url} 时出错: {e}")

    async def _send_new_posts(self, new_posts: List[dict], targets: List[str], msg_format: str, 
                               fallback_target: str = None, skip_log: bool = False):
        """发送新微博到指定目标"""
        if not targets and fallback_target:
            targets = [fallback_target]
        
        for post in new_posts:
            # 记录到每日日志
            self._log_to_daily_file(post, skip_log)
            
            content = msg_format.format(
                name=post.get("username", "未知用户"),
                weibo=post["text"],
                link=post["link"],
            )
            chain = MessageChain().message(content)
            
            send_targets = targets
            
            if not send_targets:
                self.plugin_logger.debug(f"WeiboMonitor: 没有配置推送目标，跳过推送 {post.get('username')} 的微博")
                continue
                
            sent_count = 0
            for target in send_targets:
                try:
                    await self.context.send_message(target, chain)
                    sent_count += 1
                except Exception as e:
                    self.plugin_logger.error(f"WeiboMonitor: 推送到目标 {target} 时出错: {e}")
            
            if sent_count > 0:
                self.plugin_logger.info(
                    f"WeiboMonitor: 已向 {sent_count}/{len(send_targets)} 个目标推送 {post.get('username')} 的更新"
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
            async with self._request_semaphore:
                try:
                    resp = await self.client.get(
                        f"{WEIBO_MOBILE_BASE}/n/{name}",
                        headers=self.get_headers(),
                    )
                    if resp.status_code == 429:
                        self.plugin_logger.warning(f"WeiboMonitor: 解析用户名时触发限流 (429)，等待后重试")
                        await asyncio.sleep(60)
                        resp = await self.client.get(
                            f"{WEIBO_MOBILE_BASE}/n/{name}",
                            headers=self.get_headers(),
                        )
                    final_url = str(resp.url)
                    match_uid = re.search(r"/u/(\d+)", final_url)
                    if match_uid:
                        return match_uid.group(1)
                    self.plugin_logger.debug(f"WeiboMonitor: 用户名 {name} 跳转后无法解析UID，最终URL: {final_url}")
                except Exception as e:
                    self.plugin_logger.error(f"WeiboMonitor: 解析用户名 {name} 失败: {e}")
        return None

    async def _fetch_weibo_cards(self, uid: str) -> List[dict]:
        """获取指定UID的微博卡片列表"""
        api_url = f"{WEIBO_API_BASE}?type=uid&value={uid}&containerid=107603{uid}"
        async with self._request_semaphore:
            try:
                resp = await self.client.get(api_url, headers=self.get_headers(uid))
                if resp.status_code == 429:
                    self.plugin_logger.warning(f"WeiboMonitor: 触发限流 (429)，UID: {uid}，等待 60 秒后重试")
                    await asyncio.sleep(60)
                    resp = await self.client.get(api_url, headers=self.get_headers(uid))
                if resp.status_code != 200:
                    self.plugin_logger.error(f"WeiboMonitor: 接口请求失败 (状态码 {resp.status_code}), UID: {uid}")
                    return []
                try:
                    data = resp.json()
                except ValueError as e:
                    self.plugin_logger.error(f"WeiboMonitor: 解析接口返回的JSON数据失败, UID: {uid}, 错误: {e}")
                    return []
                if data.get("ok") != 1:
                    self.plugin_logger.debug(f"WeiboMonitor: 接口返回数据状态异常, UID: {uid}")
                    return []
                return (data.get("data") or {}).get("cards", [])
            except Exception as e:
                self.plugin_logger.error(f"WeiboMonitor: 获取UID {uid} 数据时出错: {e}")
                return []

    def _extract_valid_mblogs(self, cards: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], str]:
        """从卡片列表中提取有效的微博博文，并过滤置顶"""
        valid_mblogs: List[Dict[str, Any]] = []
        username = "未知用户"
        
        for card in cards:
            if not isinstance(card, dict):
                continue
            if card.get("card_type") == 9 and isinstance(card.get("mblog"), dict):
                mblog = card["mblog"]
                # 严格的置顶过滤
                is_top = any([
                    mblog.get("isTop"),
                    mblog.get("is_top"),
                    card.get("is_top"),
                    mblog.get("top"),
                    (mblog.get("title") or {}).get("text") == "置顶"
                ])
                if is_top:
                    continue
                    
                valid_mblogs.append(mblog)
                if username == "未知用户":
                    username = (mblog.get("user") or {}).get("screen_name", "未知用户")
                    
        return valid_mblogs, username

    async def check_weibo(self, uid: str, force_fetch: bool = False) -> List[Dict[str, Any]]:
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

            valid_mblogs, username = self._extract_valid_mblogs(cards)
            if not valid_mblogs:
                self.plugin_logger.debug(f"UID {uid} ({username}) 未发现有效的微博博文")
                return []

            self.plugin_logger.debug(f"UID {uid} ({username}) 获取到 {len(valid_mblogs)} 条有效博文")

            last_id_key = f"last_id_{uid}"
            last_id_str = await self.get_kv_data(last_id_key, "0")
            last_id = int(last_id_str)

            # 初始化检查：全新监控或会话首次检查
            if not force_fetch and (last_id == 0 or uid not in self.session_initialized_uids):
                return await self._initialize_monitor(uid, username, valid_mblogs, last_id_key, last_id)

            self.session_initialized_uids.add(uid)

            # 收集新微博
            new_posts = self._collect_new_posts(uid, valid_mblogs, last_id, 
                                              force_fetch, username)

            # 更新最新ID
            if not force_fetch:
                await self._update_last_id(valid_mblogs, last_id, last_id_key)

            if new_posts:
                self.plugin_logger.info(f"UID {uid} ({username}) 发现 {len(new_posts)} 条新微博")
                new_posts.reverse()  # 按时间从旧到新排列
            else:
                self.plugin_logger.debug(f"UID {uid} ({username}) 没有新微博 (last_id: {last_id})")

            return new_posts
        except Exception as e:
            self.plugin_logger.error(f"WeiboMonitor: 检查UID {uid} 时出错: {e}")
            return []

    async def _initialize_monitor(self, uid: str, username: str, 
                                valid_mblogs: List[Dict[str, Any]], 
                                last_id_key: str, old_last_id: int) -> List[Dict[str, Any]]:
        """初始化监控状态，记录起始ID"""
        latest_id_val = valid_mblogs[0].get("id")
        if latest_id_val:
            latest_id = int(latest_id_val)
            await self.put_kv_data(last_id_key, str(latest_id))
            self.session_initialized_uids.add(uid)
            
            if old_last_id == 0:
                self.plugin_logger.info(f"WeiboMonitor: 已初始化全新监控 UID {uid} ({username})，起始 ID: {latest_id}")
                
                # 如果开启了每日日志，将获取到的历史微博记录下来
                if self.config.get("enable_daily_log", False):
                    self.plugin_logger.info(f"WeiboMonitor: 正在将 UID {uid} 的历史微博记录到日志...")
                    for mblog in reversed(valid_mblogs): # 从旧到新记录
                        text = self.clean_text(mblog.get("text", ""))
                        bid = mblog.get("bid")
                        if not bid: continue
                        link = f"{WEIBO_WEB_BASE}/{uid}/{bid}"
                        created_at_raw = mblog.get("created_at")
                        created_at = self._parse_weibo_time(created_at_raw)
                        
                        post = {
                            "text": text,
                            "link": link,
                            "username": username,
                            "created_at": created_at
                        }
                        self._log_to_daily_file(post)
            else:
                self.plugin_logger.info(f"WeiboMonitor: 已同步会话初始状态，UID {uid} ({username})，当前最新 ID: {latest_id}")
        return []

    def _collect_new_posts(self, uid: str, valid_mblogs: List[Dict[str, Any]], 
                          last_id: int, force_fetch: bool, 
                          username: str) -> List[Dict[str, Any]]:
        """收集新的微博帖子，应用屏蔽词过滤、原创/转发过滤"""
        new_posts: List[Dict[str, Any]] = []
        filter_keywords = self.config.get("filter_keywords", [])
        send_original = self.config.get("send_original", True)
        send_forward = self.config.get("send_forward", True)
        
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
                self.plugin_logger.info(f"WeiboMonitor: 微博 {current_id} 是转发微博，已根据配置跳过推送")
                continue
            if not is_forward and not send_original:
                self.plugin_logger.info(f"WeiboMonitor: 微博 {current_id} 是原创微博，已根据配置跳过推送")
                continue

            text = self.clean_text(mblog.get("text", ""))
            
            # 屏蔽词过滤（黑名单）
            if self._has_filter_keyword(text, filter_keywords, current_id):
                continue
            
            # 白名单关键词过滤（只有包含白名单关键词才推送）
            whitelist_keywords = self.config.get("whitelist_keywords", [])
            if self._should_skip_by_whitelist(text, whitelist_keywords, current_id):
                continue

            if self.config.get("enable_similarity_dedup", False) and not force_fetch:
                if self._is_duplicate(uid, text):
                    self.plugin_logger.info(f"WeiboMonitor: 微博 {current_id} 与近期推送内容相似，已跳过")
                    continue

            bid = mblog.get("bid")
            if not bid:
                self.plugin_logger.debug(f"WeiboMonitor: 微博 {current_id} 缺少bid字段，已跳过")
                continue
            link = f"{WEIBO_WEB_BASE}/{uid}/{bid}"
            
            created_at_raw = mblog.get("created_at")
            created_at = self._parse_weibo_time(created_at_raw)
            
            new_posts.append({
                "text": text, 
                "link": link, 
                "username": username,
                "created_at": created_at
            })

            if force_fetch:
                break

        if self.config.get("enable_similarity_dedup", False):
            for post in new_posts:
                self._update_similarity_cache(uid, post["text"])

        return new_posts

    def _has_filter_keyword(self, text: str, filter_keywords: List[str], post_id: int) -> bool:
        """检查文本是否包含屏蔽词"""
        for keyword in filter_keywords:
            if keyword and keyword in text:
                self.plugin_logger.info(f"WeiboMonitor: 微博 {post_id} 包含屏蔽词 '{keyword}'，已跳过推送")
                return True
        return False

    def _should_skip_by_whitelist(self, text: str, whitelist_keywords: List[str], post_id: int) -> bool:
        """检查文本是否应该被白名单过滤跳过（只有包含白名单关键词才允许推送）"""
        if not whitelist_keywords:
            return False
        for keyword in whitelist_keywords:
            if keyword and keyword in text:
                self.plugin_logger.info(f"WeiboMonitor: 微博 {post_id} 包含白名单关键词 '{keyword}'，允许推送")
                return False
        self.plugin_logger.info(f"WeiboMonitor: 微博 {post_id} 不包含任何白名单关键词，已跳过推送")
        return True

    async def _update_last_id(self, valid_mblogs: List[Dict[str, Any]], 
                             last_id: int, last_id_key: str):
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
            text = re.sub(r'<a[^>]*>全文</a>', '', text)
            
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
            text = re.sub(r'\n\s+', '\n', text)
            text = re.sub(r'\s+\n', '\n', text)
            text = re.sub(r'\n{3,}', '\n\n', text)
            
            return text.strip()
        except Exception as e:
            self.plugin_logger.error(f"WeiboMonitor: 清理文本内容失败: {e}")
            return text
