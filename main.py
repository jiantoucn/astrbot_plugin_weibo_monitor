import asyncio
import re
import httpx
import os
import json
import base64
import random
from typing import List, Optional, Tuple, Dict, Any
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger
from bs4 import BeautifulSoup

# 常量定义
DEFAULT_CHECK_INTERVAL = 10  # 默认检查间隔（分钟）
DEFAULT_REQUEST_INTERVAL = 5  # 默认请求间隔（秒）
DEFAULT_TIMEOUT = 20  # 默认HTTP请求超时（秒）
DEFAULT_MESSAGE_TEMPLATE = "🔔 {name} 发微博啦！\n\n{weibo}\n\n链接: {link}"
WEIBO_API_BASE = "https://m.weibo.cn/api/container/getIndex"
WEIBO_MOBILE_BASE = "https://m.weibo.cn"
WEIBO_WEB_BASE = "https://weibo.com"


@register("astrbot_plugin_weibo_monitor", "Sayaka", "定时监控微博用户动态并推送到指定会话。", "v1.9.0", "https://github.com/jiantoucn/astrbot_plugin_weibo_monitor")
class WeiboMonitor(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.monitor_task: Optional[asyncio.Task] = None
        
        # 检查Cookie是否配置
        cookie = self.config.get("weibo_cookie", "")
        if not cookie:
            logger.warning("WeiboMonitor: 未配置微博Cookie，插件无法正常工作！请在插件设置中填写weibo_cookie。")
        
        # 配置HTTP客户端，添加重试和超时设置
        transport = httpx.AsyncHTTPTransport(retries=2)  # 最多重试2次
        self.client = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            transport=transport,
            follow_redirects=True
        )
        self.running = True
        self.session_initialized_uids: set[str] = set()  # 跟踪本会话已初始化的UID

        # 确保数据目录存在
        self.data_dir = StarTools.get_data_dir()
        if not self.data_dir.exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_file = self.data_dir / "monitor_data.json"
        
        # 兼容旧路径迁移 (data/astrbot_plugin_weibo_monitor -> StarTools.get_data_dir())
        old_data_file = os.path.join("data", "astrbot_plugin_weibo_monitor", "monitor_data.json")
        if not self.data_file.exists() and os.path.exists(old_data_file):
            try:
                import shutil
                shutil.copy2(old_data_file, self.data_file)
                logger.info(f"WeiboMonitor: 已从旧路径迁移数据到 {self.data_file}")
            except Exception as e:
                logger.error(f"WeiboMonitor: 迁移数据失败: {e}")

        self._data = self._load_data()

        # 启动后台监控任务
        self.monitor_task = asyncio.create_task(self.run_monitor())

    def _load_data(self) -> dict:
        """从文件加载持久化数据，损坏时自动备份"""
        if self.data_file.exists():
            try:
                return json.loads(self.data_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error(f"WeiboMonitor: 加载数据文件失败: {e}")
                # 自动备份损坏的文件
                try:
                    backup_file = self.data_file.with_suffix(f".bak.{int(asyncio.get_event_loop().time())}")
                    self.data_file.rename(backup_file)
                    logger.info(f"WeiboMonitor: 已将损坏的数据文件备份为 {backup_file}")
                except Exception as backup_err:
                    logger.error(f"WeiboMonitor: 备份损坏的数据文件失败: {backup_err}")
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
            logger.error(f"WeiboMonitor: 保存数据文件失败: {e}")
            # 清理临时文件
            try:
                if temp_file.exists():
                    temp_file.unlink()
            except:
                pass

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
        logger.info("WeiboMonitor 插件已停止")

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
            logger.error(f"WeiboMonitor: 导出配置失败: {e}")
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
            logger.error(f"WeiboMonitor: 导入配置失败: {e}")
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
            logger.error(f"WeiboMonitor: 验证过程中出现错误: {e}")
            yield event.plain_result(f"❌ 验证过程中出现错误: {e}")

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
            await self._send_new_posts(latest_posts, targets, msg_format, event.unified_msg_origin)
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
                await self._send_new_posts(latest_posts, targets, msg_format, event.unified_msg_origin)
                results.append(f"✅ {latest_posts[0].get('username')} 已发送最新动态。")
            else:
                results.append(f"ℹ️ UID {uid} 未获取到有效微博。")

        yield event.plain_result("\n".join(results))

    @property
    def message_format(self) -> str:
        """获取并格式化消息模板"""
        return self.config.get(
            "message_format", DEFAULT_MESSAGE_TEMPLATE
        ).replace("\\n", "\n")

    async def run_monitor(self):
        """后台监控主循环"""
        logger.info("微博监控任务已启动")
        await asyncio.sleep(10)

        while self.running:
            try:
                urls = self._parse_urls(self.config.get("weibo_urls", []))
                
                # 检查间隔随机化
                base_interval = max(1, self.config.get("check_interval", DEFAULT_CHECK_INTERVAL))
                interval_jitter = self.config.get("check_interval_jitter", 0)
                actual_interval = max(1, random.randint(base_interval - interval_jitter, base_interval + interval_jitter))
                
                # 请求间隔参数
                base_req_interval = self.config.get("request_interval", DEFAULT_REQUEST_INTERVAL)
                req_jitter = self.config.get("request_interval_jitter", 0)
                
                targets = self.get_targets()
                msg_format = self.message_format

                cookie = self.config.get("weibo_cookie", "")
                if not cookie:
                    logger.warning("WeiboMonitor: 未配置微博Cookie，跳过本轮检查。请尽快配置！")
                elif not urls:
                    logger.debug("WeiboMonitor: 未配置监控URL")
                elif not targets:
                    logger.debug("WeiboMonitor: 未配置推送目标会话ID")
                else:
                    await self._process_monitor_cycle(urls, base_req_interval, req_jitter, targets, msg_format)

                logger.debug(f"WeiboMonitor: 下次检查将在 {actual_interval} 分钟后执行")
                await asyncio.sleep(actual_interval * 60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"WeiboMonitor 运行时错误: {e}")
                await asyncio.sleep(60)

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
                    logger.warning(f"WeiboMonitor: 无法解析URL {url}，已跳过")
                    continue

                new_posts = await self.check_weibo(uid)
                if new_posts:
                    await self._send_new_posts(new_posts, targets, msg_format)
            except Exception as e:
                logger.error(f"WeiboMonitor: 检查URL {url} 时出错: {e}")

    async def _send_new_posts(self, new_posts: List[dict], targets: List[str], msg_format: str, 
                               fallback_target: str = None):
        """发送新微博到指定目标"""
        if not targets and fallback_target:
            targets = [fallback_target]
        
        for post in new_posts:
            content = msg_format.format(
                name=post.get("username", "未知用户"),
                weibo=post["text"],
                link=post["link"],
            )
            chain = MessageChain().message(content)
            
            send_targets = targets
            
            if not send_targets:
                logger.debug(f"WeiboMonitor: 没有配置推送目标，跳过推送 {post.get('username')} 的微博")
                continue
                
            sent_count = 0
            for target in send_targets:
                try:
                    await self.context.send_message(target, chain)
                    sent_count += 1
                except Exception as e:
                    logger.error(f"WeiboMonitor: 推送到目标 {target} 时出错: {e}")
            
            if sent_count > 0:
                logger.info(
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

        # 匹配 /u/UID 格式
        match = re.search(r"weibo\.(com|cn)/u/(\d+)", url)
        if match:
            return match.group(2)

        # 匹配 /n/用户名 格式
        match_name = re.search(r"weibo\.(com|cn)/n/([^/?#]+)", url)
        if match_name:
            name = match_name.group(2)
            try:
                # 请求跳转接口
                resp = await self.client.get(
                    f"{WEIBO_MOBILE_BASE}/n/{name}",
                    headers=self.get_headers(),
                )
                # 从最终跳转的URL中提取UID
                final_url = str(resp.url)
                match_uid = re.search(r"/u/(\d+)", final_url)
                if match_uid:
                    return match_uid.group(1)
                logger.debug(f"WeiboMonitor: 用户名 {name} 跳转后无法解析UID，最终URL: {final_url}")
            except Exception as e:
                logger.error(f"WeiboMonitor: 解析用户名 {name} 失败: {e}")
        return None

    async def _fetch_weibo_cards(self, uid: str) -> List[dict]:
        """获取指定UID的微博卡片列表"""
        api_url = f"{WEIBO_API_BASE}?type=uid&value={uid}&containerid=107603{uid}"
        try:
            resp = await self.client.get(api_url, headers=self.get_headers(uid))
            if resp.status_code != 200:
                logger.error(f"WeiboMonitor: 接口请求失败 (状态码 {resp.status_code}), UID: {uid}")
                return []
            try:
                data = resp.json()
            except ValueError as e:
                logger.error(f"WeiboMonitor: 解析接口返回的JSON数据失败, UID: {uid}, 错误: {e}")
                return []
            if data.get("ok") != 1:
                logger.debug(f"WeiboMonitor: 接口返回数据状态异常, UID: {uid}")
                return []
            return (data.get("data") or {}).get("cards", [])
        except Exception as e:
            logger.error(f"WeiboMonitor: 获取UID {uid} 数据时出错: {e}")
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
            cards = await self._fetch_weibo_cards(uid)
            if not cards:
                return []

            valid_mblogs, username = self._extract_valid_mblogs(cards)
            if not valid_mblogs:
                return []

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
                new_posts.reverse()  # 按时间从旧到新排列

            return new_posts
        except Exception as e:
            logger.error(f"WeiboMonitor: 检查UID {uid} 时出错: {e}")
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
                logger.info(f"WeiboMonitor: 已初始化全新监控 UID {uid} ({username})，起始 ID: {latest_id}")
            else:
                logger.info(f"WeiboMonitor: 已同步会话初始状态，UID {uid} ({username})，当前最新 ID: {latest_id}")
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
                logger.info(f"WeiboMonitor: 微博 {current_id} 是转发微博，已根据配置跳过推送")
                continue
            if not is_forward and not send_original:
                logger.info(f"WeiboMonitor: 微博 {current_id} 是原创微博，已根据配置跳过推送")
                continue

            text = self.clean_text(mblog.get("text", ""))
            
            # 屏蔽词过滤（黑名单）
            if self._has_filter_keyword(text, filter_keywords, current_id):
                continue
            
            # 白名单关键词过滤（只有包含白名单关键词才推送）
            whitelist_keywords = self.config.get("whitelist_keywords", [])
            if self._should_skip_by_whitelist(text, whitelist_keywords, current_id):
                continue

            bid = mblog.get("bid")
            if not bid:
                logger.debug(f"WeiboMonitor: 微博 {current_id} 缺少bid字段，已跳过")
                continue
            link = f"{WEIBO_WEB_BASE}/{uid}/{bid}"
            new_posts.append({"text": text, "link": link, "username": username})

            if force_fetch:  # 强制获取模式只取第一条
                break

        return new_posts

    def _has_filter_keyword(self, text: str, filter_keywords: List[str], post_id: int) -> bool:
        """检查文本是否包含屏蔽词"""
        for keyword in filter_keywords:
            if keyword and keyword in text:
                logger.info(f"WeiboMonitor: 微博 {post_id} 包含屏蔽词 '{keyword}'，已跳过推送")
                return True
        return False

    def _should_skip_by_whitelist(self, text: str, whitelist_keywords: List[str], post_id: int) -> bool:
        """检查文本是否应该被白名单过滤跳过（只有包含白名单关键词才允许推送）"""
        if not whitelist_keywords:
            return False
        for keyword in whitelist_keywords:
            if keyword and keyword in text:
                logger.info(f"WeiboMonitor: 微博 {post_id} 包含白名单关键词 '{keyword}'，允许推送")
                return False
        logger.info(f"WeiboMonitor: 微博 {post_id} 不包含任何白名单关键词，已跳过推送")
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
            logger.error(f"WeiboMonitor: 清理文本内容失败: {e}")
            return text
