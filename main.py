import asyncio
import re
import httpx
import os
import json
import base64
import random
import tempfile
import sys
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api.message_components import Image, Node, Nodes, Plain, Video
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


@register("astrbot_plugin_weibo_monitor", "Sayaka", "定时监控微博用户动态并推送到指定会话。", "v1.8.1", "https://github.com/jiantoucn/astrbot_plugin_weibo_monitor")
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
        
        # 兼容旧路径迁移
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
        # 启动 Playwright 自动安装任务
        self.playwright_init_task = asyncio.create_task(self._init_playwright())

    async def _init_playwright(self):
        """异步安装 Playwright 及 Chromium，使用持久化路径"""
        browser_dir = StarTools.get_data_dir() / "playwright_browsers"
        if not browser_dir.exists():
            browser_dir.mkdir(parents=True, exist_ok=True)
        
        # 设置持久化环境变量
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_dir)
        
        # 检查 playwright 库是否安装
        try:
            import playwright
        except ImportError:
            logger.info("WeiboMonitor: 未检测到 Playwright 运行库，开始自动安装...")
            proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "pip", "install", "playwright")
            await proc.communicate()
            
        # 检查浏览器内核是否已下载 (通过检查目录是否为空)
        if not any(browser_dir.iterdir()):
            logger.info("WeiboMonitor: 正在初始化 Playwright Chromium 浏览器内核，首次下载可能需要几分钟，请耐心等待...")
            proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "playwright", "install", "chromium")
            await proc.communicate()
            logger.info("WeiboMonitor: Playwright 浏览器依赖安装完成！")

    def _load_data(self) -> dict:
        if self.data_file.exists():
            try:
                return json.loads(self.data_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error(f"WeiboMonitor: 加载数据文件失败: {e}")
                try:
                    backup_file = self.data_file.with_suffix(f".bak.{int(asyncio.get_event_loop().time())}")
                    self.data_file.rename(backup_file)
                    logger.info(f"WeiboMonitor: 已将损坏的数据文件备份为 {backup_file}")
                except Exception as backup_err:
                    logger.error(f"WeiboMonitor: 备份损坏的数据文件失败: {backup_err}")
        return {}

    def _save_data(self):
        try:
            temp_file = self.data_file.with_suffix(".tmp")
            temp_file.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=4), 
                encoding="utf-8"
            )
            temp_file.replace(self.data_file)
        except Exception as e:
            logger.error(f"WeiboMonitor: 保存数据文件失败: {e}")
            try:
                if temp_file.exists():
                    temp_file.unlink()
            except:
                pass

    async def get_kv_data(self, key: str, default=None):
        return self._data.get(key, default)

    async def put_kv_data(self, key: str, value):
        self._data[key] = value
        self._save_data()

    def get_headers(self, uid: str = "") -> Dict[str, str]:
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
        if not config_str:
            yield event.plain_result("❌ 请提供配置字符串。用法: /weibo_import <配置字符串>")
            return

        try:
            try:
                decoded = base64.b64decode(config_str).decode("utf-8")
                new_config = json.loads(decoded)
            except Exception:
                new_config = json.loads(config_str)

            if not isinstance(new_config, dict):
                raise ValueError("配置格式不正确")

            count = 0
            for key, value in new_config.items():
                self.config[key] = value
                count += 1

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
        urls = self._parse_urls(self.config.get("weibo_urls", []))
        if not urls:
            yield event.plain_result("❌ 未在插件设置中配置监控URL。")
            return
            
        yield event.plain_result(f"🔍 正在检查首个微博账号的最新动态...")
        
        url = urls[0]
        targets = self.get_targets()
        msg_format = self.message_format
        
        uid = await self.parse_uid(url)
        if not uid:
            yield event.plain_result(f"❌ 无法解析URL: {url}")
            return

        latest_posts = await self.check_weibo(uid, force_fetch=True)
        if latest_posts:
            post = latest_posts[0]
            actual_targets = targets if targets else [event.unified_msg_origin]
            await self._send_new_posts([post], actual_targets, msg_format)
            yield event.plain_result(f"✅ {post.get('username')} 已成功发送最新动态。")
        else:
            yield event.plain_result(f"ℹ️ UID {uid} 未获取到有效微博。")

    @filter.command("weibo_check_all")
    async def weibo_check_all(self, event: AstrMessageEvent):
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
                post = latest_posts[0]
                actual_targets = targets if targets else [event.unified_msg_origin]
                await self._send_new_posts([post], actual_targets, msg_format)
                results.append(f"✅ {post.get('username')} 已成功发送最新动态。")
            else:
                results.append(f"ℹ️ UID {uid} 未获取到有效微博。")

        yield event.plain_result("\n".join(results))

    @property
    def message_format(self) -> str:
        return self.config.get(
            "message_format", DEFAULT_MESSAGE_TEMPLATE
        ).replace("\\n", "\n")

    async def run_monitor(self):
        logger.info("微博监控任务已启动")
        await asyncio.sleep(10)

        while self.running:
            try:
                urls = self._parse_urls(self.config.get("weibo_urls", []))
                
                base_interval = max(1, self.config.get("check_interval", DEFAULT_CHECK_INTERVAL))
                interval_jitter = self.config.get("check_interval_jitter", 0)
                actual_interval = max(1, random.randint(base_interval - interval_jitter, base_interval + interval_jitter))
                
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
        if isinstance(urls_raw, str):
            return [u.strip() for u in urls_raw.split(",") if u.strip()]
        
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

    async def _take_screenshot(self, url: str) -> Optional[str]:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning("WeiboMonitor: 未安装 playwright，无法截图。请等待后台自动安装任务完成。")
            return None

        screenshot_cfg: Dict[str, Any] = {}
        try:
            wa_data_dir = Path("data") / "plugin_data" / "astrbot_plugin_web_analyzer"
            wa_cfg_file = wa_data_dir / "config.json"
            if wa_cfg_file.exists():
                screenshot_cfg = json.loads(wa_cfg_file.read_text(encoding="utf-8"))
        except Exception:
            pass

        width        = int(screenshot_cfg.get("screenshot_width")  or self.config.get("screenshot_width",  1280))
        height       = int(screenshot_cfg.get("screenshot_height") or self.config.get("screenshot_height", 720))
        quality      = int(screenshot_cfg.get("screenshot_quality") or self.config.get("screenshot_quality", 80))
        wait_ms      = int(screenshot_cfg.get("screenshot_wait_time") or self.config.get("screenshot_wait_time", 2000))
        full_page    = bool(screenshot_cfg.get("screenshot_full_page") or self.config.get("screenshot_full_page", False))
        fmt          = str(screenshot_cfg.get("screenshot_format") or self.config.get("screenshot_format", "jpeg")).lower()
        if fmt not in ("jpeg", "png"):
            fmt = "jpeg"

        tmp_file = tempfile.NamedTemporaryFile(suffix=f".{fmt}", delete=False)
        tmp_path = tmp_file.name
        tmp_file.close()

        try:
            async with async_playwright() as p:
                launch_kwargs: Dict[str, Any] = {
                    "args": ["--no-sandbox", "--disable-setuid-sandbox"],
                }

                browser = await p.chromium.launch(**launch_kwargs)
                page = await browser.new_page(viewport={"width": width, "height": height})

                cookie_str = self.config.get("weibo_cookie", "")
                if cookie_str:
                    cookies = []
                    for part in cookie_str.split(";"):
                        part = part.strip()
                        if "=" in part:
                            name, _, value = part.partition("=")
                            cookies.append({
                                "name": name.strip(),
                                "value": value.strip(),
                                "domain": ".weibo.com",
                                "path": "/",
                            })
                    if cookies:
                        await page.context.add_cookies(cookies)

                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(wait_ms)

                shot_kwargs: Dict[str, Any] = {
                    "path": tmp_path,
                    "full_page": full_page,
                    "type": fmt,
                }
                if fmt == "jpeg":
                    shot_kwargs["quality"] = quality

                await page.screenshot(**shot_kwargs)
                await browser.close()

            logger.info(f"WeiboMonitor: 截图成功 -> {tmp_path}")
            return tmp_path

        except Exception as e:
            logger.error(f"WeiboMonitor: 截图失败 ({url}): {e}")
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            return None

    async def _download_to_tmp(self, url: str, suffix: str) -> Optional[str]:
        """下载媒体文件到临时文件，返回路径"""
        try:
            resp = await self.client.get(url, headers=self.get_headers(), follow_redirects=True)
            if resp.status_code != 200:
                return None
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            tmp.write(resp.content)
            tmp.close()
            return tmp.name
        except Exception as e:
            logger.error(f"WeiboMonitor: 下载媒体失败 ({url}): {e}")
            return None

    async def _send_new_posts(self, new_posts: List[dict], targets: List[str], msg_format: str):
        enable_screenshot = self.config.get("weibo_screenshot", True)

        for post in new_posts:
            content = msg_format.format(
                name=post.get("username", "未知用户"),
                weibo=post["text"],
                link=post["link"],
            )
            image_urls: List[str] = post.get("image_urls") or []
            video_url: Optional[str] = post.get("video_url")
            tmp_files: List[str] = []

            try:
                # 根据媒体类型构建消息
                if video_url:
                    # AstrBot 文档建议视频优先使用 URL 发送，避免 fromFileSystem
                    # 受限于机器人端文件系统可见性，且规避延迟读取临时文件导致的 ENOENT。
                    media_components = [Video.fromURL(video_url)]
                    nodes_list = [Node(uin="0", name=post.get("username", "微博"), content=[Plain(content)])]
                    if media_components:
                        nodes_list.append(Node(uin="0", name=post.get("username", "微博"), content=media_components))
                    chain = MessageChain()
                    chain.chain.append(Nodes(nodes=nodes_list))
                elif image_urls:
                    # 文字+图片：合并转发
                    img_components = []
                    for img_url in image_urls:
                        img_path = await self._download_to_tmp(img_url, ".jpg")
                        if img_path:
                            tmp_files.append(img_path)
                            img_components.append(Image.fromFileSystem(img_path))
                    nodes_list = [Node(uin="0", name=post.get("username", "微博"), content=[Plain(content)])]
                    if img_components:
                        nodes_list.append(Node(uin="0", name=post.get("username", "微博"), content=img_components))
                    chain = MessageChain()
                    chain.chain.append(Nodes(nodes=nodes_list))
                else:
                    # 纯文字：单 Node 转发
                    chain = MessageChain()
                    chain.chain.append(Node(uin="0", name=post.get("username", "微博"), content=[Plain(content)]))

                # 截图开关独立：无论何种媒体类型，开启时额外附加截图
                if enable_screenshot:
                    screenshot_path = await self._take_screenshot(post["link"])
                    if screenshot_path:
                        tmp_files.append(screenshot_path)
                        try:
                            chain.chain.append(Image.fromFileSystem(screenshot_path))
                        except Exception as e:
                            logger.warning(f"WeiboMonitor: 截图附加失败: {e}")

                sent_count = 0
                for target in targets:
                    try:
                        await self.context.send_message(target, chain)
                        sent_count += 1
                    except Exception as e:
                        logger.error(f"WeiboMonitor: 推送到 {target} 失败: {e}")

                if sent_count > 0:
                    logger.info(f"WeiboMonitor: 已向 {sent_count}/{len(targets)} 个目标推送 {post.get('username')} 的更新")
            finally:
                for f in tmp_files:
                    try:
                        os.unlink(f)
                    except Exception:
                        pass

    async def parse_uid(self, url: str) -> Optional[str]:
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
                resp = await self.client.get(
                    f"{WEIBO_MOBILE_BASE}/n/{name}",
                    headers=self.get_headers(),
                )
                final_url = str(resp.url)
                match_uid = re.search(r"/u/(\d+)", final_url)
                if match_uid:
                    return match_uid.group(1)
                logger.debug(f"WeiboMonitor: 用户名 {name} 跳转后无法解析UID，最终URL: {final_url}")
            except Exception as e:
                logger.error(f"WeiboMonitor: 解析用户名 {name} 失败: {e}")
        return None

    async def _fetch_weibo_cards(self, uid: str) -> List[dict]:
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
        valid_mblogs: List[Dict[str, Any]] = []
        username = "未知用户"
        
        for card in cards:
            if not isinstance(card, dict):
                continue
            if card.get("card_type") == 9 and isinstance(card.get("mblog"), dict):
                mblog = card["mblog"]
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

            if not force_fetch and (last_id == 0 or uid not in self.session_initialized_uids):
                return await self._initialize_monitor(uid, username, valid_mblogs, last_id_key, last_id)

            self.session_initialized_uids.add(uid)

            new_posts = self._collect_new_posts(uid, valid_mblogs, last_id, 
                                              force_fetch, username)

            if not force_fetch:
                await self._update_last_id(valid_mblogs, last_id, last_id_key)

            if new_posts:
                new_posts.reverse()

            return new_posts
        except Exception as e:
            logger.error(f"WeiboMonitor: 检查UID {uid} 时出错: {e}")
            return []

    async def _initialize_monitor(self, uid: str, username: str, 
                                valid_mblogs: List[Dict[str, Any]], 
                                last_id_key: str, old_last_id: int) -> List[Dict[str, Any]]:
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
        new_posts: List[Dict[str, Any]] = []
        filter_keywords = self.config.get("filter_keywords", [])
        send_original = self.config.get("send_original", True)
        send_forward = self.config.get("send_forward", True)
        
        for mblog in valid_mblogs:
            current_id_val = mblog.get("id")
            if not current_id_val:
                continue
                
            current_id = int(current_id_val)
            
            if not force_fetch and current_id <= last_id:
                break

            is_forward = "retweeted_status" in mblog
            if is_forward and not send_forward:
                logger.info(f"WeiboMonitor: 微博 {current_id} 是转发微博，已根据配置跳过推送")
                continue
            if not is_forward and not send_original:
                logger.info(f"WeiboMonitor: 微博 {current_id} 是原创微博，已根据配置跳过推送")
                continue

            text = self.clean_text(mblog.get("text", ""))
            
            if self._has_filter_keyword(text, filter_keywords, current_id):
                continue

            whitelist_keywords = self.config.get("whitelist_keywords", [])
            if whitelist_keywords and not any(kw and kw in text for kw in whitelist_keywords):
                logger.info(f"WeiboMonitor: 微博 {current_id} 不含白名单关键词，已跳过推送")
                continue

            bid = mblog.get("bid")
            if not bid:
                logger.debug(f"WeiboMonitor: 微博 {current_id} 缺少bid字段，已跳过")
                continue
            link = f"{WEIBO_WEB_BASE}/{uid}/{bid}"

            # 提取图片列表
            pics = mblog.get("pics") or []
            image_urls = [p["large"]["url"] for p in pics if isinstance(p, dict) and p.get("large", {}).get("url")]

            # 提取视频URL
            video_url = None
            page_info = mblog.get("page_info") or {}
            if page_info.get("type") == "video":
                media_info = page_info.get("media_info") or {}
                video_url = media_info.get("stream_url_hd") or media_info.get("stream_url")

            new_posts.append({"text": text, "link": link, "username": username,
                               "image_urls": image_urls, "video_url": video_url})

            if force_fetch:
                break

        return new_posts

    def _has_filter_keyword(self, text: str, filter_keywords: List[str], post_id: int) -> bool:
        for keyword in filter_keywords:
            if keyword and keyword in text:
                logger.info(f"WeiboMonitor: 微博 {post_id} 包含屏蔽词 '{keyword}'，已跳过推送")
                return True
        return False

    async def _update_last_id(self, valid_mblogs: List[Dict[str, Any]], 
                             last_id: int, last_id_key: str):
        latest_id_val = valid_mblogs[0].get("id")
        if latest_id_val:
            latest_id = int(latest_id_val)
            if latest_id > last_id:
                await self.put_kv_data(last_id_key, str(latest_id))

    def clean_text(self, text: str) -> str:
        if not text:
            return ""
        if not isinstance(text, str):
            return str(text)
            
        try:
            text = re.sub(r'<a[^>]*>全文</a>', '', text)
            
            soup = BeautifulSoup(text, "html.parser")
            
            for img in soup.find_all("img"):
                alt = img.get("alt", "")
                if alt:
                    img.replace_with(alt)
            
            for a in soup.find_all("a"):
                link_text = a.get_text()
                a.replace_with(link_text)
            
            for br in soup.find_all("br"):
                br.replace_with("\n")
            
            text = soup.get_text()
            
            text = re.sub(r'\n\s+', '\n', text)
            text = re.sub(r'\s+\n', '\n', text)
            text = re.sub(r'\n{3,}', '\n\n', text)
            
            return text.strip()
        except Exception as e:
            logger.error(f"WeiboMonitor: 清理文本内容失败: {e}")
            return text
