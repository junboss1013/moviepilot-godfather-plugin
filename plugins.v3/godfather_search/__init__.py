"""
教父资源转存插件（MoviePilot V3 / V2 双兼容）。

三级交互（电影/多季分开选，资源列表分页，不自动下载）：
  1. 发送「影片名？」→ 自动登录教父站，模糊搜索并在聊天框展示【影视列表】
     （电影、第一季、第二季… 各自独立，带序号）；
  2. 回复影视序号 → 精确搜索该影视详情页的全部资源，展示【资源列表】
     （带全局序号，每页 8 条，可「上一页/下一页」翻页，「返回」换影视）；
  3. 回复资源序号（带不带问号均可）→ 才把对应资源转存到 115 网盘。

合规说明：所有登录凭据（账号密码/Cookie）由用户自行提供，插件不破解鉴权、不撞库。
"""
import re
import time
from typing import Any, Dict, List, Optional, Tuple

# V3 优先使用稳定 SDK；回退到 V2 旧路径，实现一份代码 V2/V3 通吃
try:
    from app.sdk.plugin import _PluginBase
    from app.sdk.events import eventmanager, Event
    from app.sdk.logging import logger
except (ImportError, ModuleNotFoundError):
    from app.core.event import eventmanager, Event
    from app.log import logger
    from app.plugins import _PluginBase

from app.schemas.types import EventType

from .godfather_client import GodfatherClient
from .pan115_client import Pan115Client
from .resource_parser import sort_resources
from .media_identifier import identify_media, build_classify_path


class godfather_search(_PluginBase):
    # ---------------- 插件元信息 ----------------
    plugin_name = "教父资源转存"
    plugin_desc = "三级交互：片名？→影视列表(电影/各季分开)→回复影视序号看资源列表(可翻页)→回复资源序号才转存115网盘。"
    plugin_icon = "godfather.png"
    plugin_version = "3.5.1"
    plugin_author = "WorkBuddy"
    author_url = "https://github.com"
    plugin_config_prefix = "godfather_search_"
    plugin_order = 10
    auth_level = 1

    # ---------------- 运行时状态 ----------------
    def __init__(self):
        super().__init__()
        self._enabled = False
        self._site_username = ""
        self._site_password = ""
        self._site_cookie = ""
        self._site_url = GodfatherClient.DEFAULT_BASE
        self._use_browser = True
        self._115_cookie = ""
        self._quality_weight = 1.0
        self._seeders_weight = 1.0
        self._max_results = 8
        self._trigger_suffix = "？"
        self._auto_transfer = True
        # 自动分类：115 网盘内建 电影/电视剧 目录，配合 rclone 挂载供 Emby 刮削
        self._auto_classify = True
        self._classify_root = "影视库"
        self._movie_dir = "电影"
        self._tv_dir = "电视剧"

        self._site_client: Optional[GodfatherClient] = None
        self._pan115: Optional[Pan115Client] = None
        # 每页展示条数（资源列表分页用）
        self._page_size = 8
        # 用户会话缓存（两级状态机，30 分钟过期）：
        #   stage=media  ：{'stage','keyword','media_list','timestamp'}
        #   stage=resource：{'stage','keyword','media','resources','page','timestamp'}
        self._cache: Dict[str, Dict[str, Any]] = {}

    # ---------------- 生命周期 ----------------
    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._site_username = config.get("site_username", "").strip()
        self._site_password = config.get("site_password", "").strip()
        self._site_cookie = config.get("site_cookie", "").strip()
        self._site_url = config.get("site_url") or GodfatherClient.DEFAULT_BASE
        self._use_browser = bool(config.get("use_browser", True))
        self._115_cookie = config.get("115_cookie", "").strip()
        self._quality_weight = float(config.get("quality_weight", 1.0) or 1.0)
        self._seeders_weight = float(config.get("seeders_weight", 1.0) or 1.0)
        self._max_results = int(config.get("max_results", 8) or 8)
        self._trigger_suffix = config.get("trigger_suffix") or "？"
        self._auto_transfer = bool(config.get("auto_transfer", True))
        self._auto_classify = bool(config.get("auto_classify", True))
        self._classify_root = (config.get("classify_root") or "影视库").strip()
        self._movie_dir = (config.get("movie_dir") or "电影").strip()
        self._tv_dir = (config.get("tv_dir") or "电视剧").strip()

        self._site_client = None
        self._pan115 = None
        if self._enabled:
            try:
                self._site_client = GodfatherClient(
                    cookie=self._site_cookie,
                    username=self._site_username,
                    password=self._site_password,
                    base_url=self._site_url,
                    use_browser=self._use_browser,
                )
            except Exception as e:
                logger.error(f"教父站客户端初始化失败: {e}")
            try:
                self._pan115 = Pan115Client(cookie=self._115_cookie)
            except Exception as e:
                logger.error(f"115 客户端初始化失败: {e}")
        logger.info(f"{self.plugin_name} v{self.plugin_version} 初始化完成, enabled={self._enabled}")

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self):
        for client in (self._site_client, self._pan115):
            if client:
                try:
                    client.close()
                except Exception:
                    pass
        self._cache.clear()

    # ---------------- 标准接口 ----------------
    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_page(self) -> List[dict]:
        return []

    # ---------------- 配置表单 ----------------
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VAlert",
                                "props": {
                                    "type": "info", "variant": "tonal",
                                    "text": "三步操作：① 发「片名？」展示影视列表（电影/各季分开）；② 回复影视序号看该影视的资源列表（每页8条，发「下一页/上一页」翻页，「返回」换影视）；③ 回复资源序号（如 7，带不带问号均可）才转存115网盘。不会自动下载。请填教父站账号密码（自动登录，兼容 Cookie）与115 Cookie。",
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12, "md": 6},
                            "content": [{
                                "component": "VSwitch",
                                "props": {"model": "enabled", "label": "启用插件"},
                            }],
                        }, {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 6},
                            "content": [{
                                "component": "VSwitch",
                                "props": {
                                    "model": "use_browser", "label": "浏览器验证(PoW)",
                                    "hint": "站点有人机验证时，用内置浏览器自动完成（需 playwright+chromium）",
                                    "persistent-hint": True,
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VTextField",
                                "props": {
                                    "model": "site_url", "label": "教父站地址",
                                    "placeholder": "https://www.xn--wcv59z.com",
                                    "hint": "站点首页地址，默认 www.教父.com 的 punycode",
                                    "persistent-hint": True, "clearable": True,
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12, "md": 6},
                            "content": [{
                                "component": "VTextField",
                                "props": {
                                    "model": "site_username", "label": "教父站账号 *",
                                    "placeholder": "用户名 / 邮箱",
                                    "hint": "教父站登录账号（用户名或邮箱）",
                                    "persistent-hint": True, "clearable": True,
                                },
                            }],
                        }, {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 6},
                            "content": [{
                                "component": "VTextField",
                                "props": {
                                    "model": "site_password", "label": "教父站密码 *",
                                    "placeholder": "登录密码",
                                    "hint": "教父站登录密码，仅保存在本机插件配置",
                                    "persistent-hint": True, "type": "password", "clearable": True,
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VTextField",
                                "props": {
                                    "model": "115_cookie", "label": "115 网盘 Cookie *",
                                    "placeholder": "115 网盘登录 Cookie（含 UID/CID/SEID/KID）",
                                    "hint": "必填。用于转存分享与添加离线下载",
                                    "persistent-hint": True, "type": "password", "clearable": True,
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [{
                                "component": "VTextField",
                                "props": {
                                    "model": "max_results", "label": "展示条数",
                                    "placeholder": "8", "type": "number", "min": 1, "max": 20,
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [{
                                "component": "VTextField",
                                "props": {
                                    "model": "quality_weight", "label": "画质权重",
                                    "placeholder": "1.0", "type": "number",
                                    "hint": "越大越偏好高画质", "persistent-hint": True,
                                },
                            }],
                        }, {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [{
                                "component": "VTextField",
                                "props": {
                                    "model": "seeders_weight", "label": "做种权重",
                                    "placeholder": "1.0", "type": "number",
                                    "hint": "越大越偏好做种多", "persistent-hint": True,
                                },
                            }],
                        }, {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [{
                                "component": "VTextField",
                                "props": {
                                    "model": "trigger_suffix", "label": "触发后缀",
                                    "placeholder": "？", "hint": "默认中文问号，也支持英文 ?",
                                    "persistent-hint": True,
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12, "md": 6},
                            "content": [{
                                "component": "VSwitch",
                                "props": {
                                    "model": "auto_classify", "label": "转存时自动分类(电影/电视剧)",
                                    "hint": "在115网盘自动建 影视库/电影/片名(年份)、影视库/电视剧/片名/Season XX，配合rclone挂载供Emby分别刮削",
                                    "persistent-hint": True,
                                },
                            }],
                        }, {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 6},
                            "content": [{
                                "component": "VTextField",
                                "props": {
                                    "model": "classify_root", "label": "分类根目录(115网盘内)",
                                    "placeholder": "影视库",
                                    "hint": "Emby 添加媒体库时分别指向此目录下的 电影 / 电视剧 文件夹",
                                    "persistent-hint": True, "clearable": True,
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12, "md": 6},
                            "content": [{
                                "component": "VTextField",
                                "props": {
                                    "model": "movie_dir", "label": "电影目录名",
                                    "placeholder": "电影", "clearable": True,
                                },
                            }],
                        }, {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 6},
                            "content": [{
                                "component": "VTextField",
                                "props": {
                                    "model": "tv_dir", "label": "电视剧目录名",
                                    "placeholder": "电视剧", "clearable": True,
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VAlert",
                                "props": {
                                    "type": "warning", "variant": "tonal",
                                    "text": "合规提示：请确保你有权访问相关内容，并遵守所在地区法律法规及站点条款。插件仅自动化你本人账号的转存操作。",
                                },
                            }],
                        }],
                    },
                ],
            },
        ], {
            "enabled": False,
            "site_url": GodfatherClient.DEFAULT_BASE,
            "site_username": "",
            "site_password": "",
            "site_cookie": "",
            "use_browser": True,
            "115_cookie": "",
            "quality_weight": 1.0,
            "seeders_weight": 1.0,
            "max_results": 8,
            "trigger_suffix": "？",
            "auto_classify": True,
            "classify_root": "影视库",
            "movie_dir": "电影",
            "tv_dir": "电视剧",
        }

    # ---------------- 消息监听 ----------------
    @eventmanager.register(EventType.UserMessage)
    def talk(self, event: Event):
        if not self._enabled:
            return
        event_data = event.event_data or {}
        text = (event_data.get("text") or "").strip()
        if not text:
            return
        userid = event_data.get("userid")
        channel = event_data.get("channel")

        clean = text.rstrip("？?").strip()
        if not clean:
            return

        cache = self._cache.get(userid)
        if cache and time.time() - cache.get("timestamp", 0) > 1800:
            self._cache.pop(userid, None)
            cache = None

        # ① 翻页指令：上一页 / 下一页（带不带问号均可），仅资源列表阶段有效
        if clean in ("上一页", "下一页", "上页", "下页", "前一页", "后一页"):
            if cache and cache.get("stage") == "resource":
                delta = -1 if clean in ("上一页", "上页", "前一页") else 1
                self._handle_page(delta, channel, userid)
            else:
                self.post_message(channel=channel, title="提示",
                                  text="请先发送「片名？」并选择一部影视。", userid=userid)
            return

        # ② 返回影视列表：input「返回 / 上一步 / 影视列表」
        if clean in ("返回", "返回列表", "上一步", "影视列表", "重新选择"):
            if cache and cache.get("stage") == "resource":
                self._show_media_list(userid, channel, cache)
            else:
                self.post_message(channel=channel, title="提示",
                                  text="请先发送「片名？」搜索影视。", userid=userid)
            return

        # ③ 纯数字：按当前阶段处理（选影视 / 选资源转存）
        if clean.isdigit():
            if not cache:
                self.post_message(channel=channel, title="提示",
                                  text="请先发送「片名？」搜索，再回复序号。", userid=userid)
                return
            if cache.get("stage") == "media":
                self._handle_select_media(int(clean), channel, userid)
            else:
                self._handle_pick(int(clean), channel, userid)
            return

        # ④ 关键词搜索：以配置的问号结尾（同时兼容英文 ?）
        if not (text.endswith("？") or text.endswith("?")):
            return
        self._handle_search(clean, channel, userid)

    # ---------------- 核心流程 ----------------
    def _configure_ok(self, channel, userid) -> bool:
        """公共前置校验：客户端与登录凭据。"""
        if not self._site_client:
            self.post_message(channel=channel, title="配置错误",
                              text="教父站客户端未初始化，请检查是否启用插件。", userid=userid)
            return False
        if not (self._site_username and self._site_password) and not self._site_cookie:
            self.post_message(channel=channel, title="缺少登录凭据",
                              text="请先配置教父站账号和密码（或旧版 Cookie）再搜索。", userid=userid)
            return False
        return True

    @staticmethod
    def _media_card_label(title: str) -> str:
        """影视卡片展示名：直接用站点真实标题，仅把结尾裸年份规范为 (YYYY)。
        绝不用搜索关键词覆盖——否则模糊搜索出的多部不同影片会被显示成同一个名字。"""
        raw = (title or "未命名影视").strip()
        mm = re.search(r"^(.*?)[\s._-]+((?:19|20)\d{2})\s*$", raw)
        return f"{mm.group(1).strip()} ({mm.group(2)})" if mm else raw

    def _handle_search(self, keyword: str, channel, userid):
        """第一级：模糊搜索 → 展示影视列表（电影 / 各季分开），不取资源。"""
        self.post_message(channel=channel, title="教父搜索",
                          text=f"🔍 正在搜索「{keyword}」的影视列表…", userid=userid)
        if not self._configure_ok(channel, userid):
            return

        media_list, msg = self._site_client.search_media_list(keyword)
        if not media_list:
            self.post_message(channel=channel, title="无结果",
                              text=f"❌ {msg}", userid=userid)
            return

        self._cache[userid] = {
            "stage": "media", "keyword": keyword,
            "media_list": media_list, "timestamp": time.time(),
        }
        lines = [
            f"🎬 「{keyword}」影视列表（共 {len(media_list)} 部）：", "",
        ]
        for i, m in enumerate(media_list, 1):
            # 直接展示站点卡片真实标题，不用搜索关键词覆盖片名
            lines.append(f"{i}. {self._media_card_label(m.get('title'))}")
        lines += ["", "💡 回复影视序号查看其资源列表；如「1」；发「片名？」重新搜索。"]
        self.post_message(channel=channel, title="影视列表",
                          text="\n".join(lines), userid=userid)

    def _show_media_list(self, userid, channel, cache: Dict):
        """从资源阶段返回影视列表。"""
        media_list = cache.get("media_list", [])
        keyword = cache.get("keyword", "")
        lines = [f"🎬 「{keyword}」影视列表（共 {len(media_list)} 部）：", ""]
        for i, m in enumerate(media_list, 1):
            lines.append(f"{i}. {self._media_card_label(m.get('title'))}")
        lines += ["", "💡 回复影视序号查看其资源列表。"]
        self.post_message(channel=channel, title="影视列表",
                          text="\n".join(lines), userid=userid)

    def _handle_select_media(self, number: int, channel, userid):
        """第二级：按影视序号 → 精确取该影视资源 → 展示第 1 页。"""
        cache = self._cache.get(userid, {})
        media_list = cache.get("media_list", [])
        if not (1 <= number <= len(media_list)):
            self.post_message(channel=channel, title="序号错误",
                              text=f"请输入 1-{len(media_list)} 之间的影视序号。", userid=userid)
            return
        media = media_list[number - 1]
        title = media.get("title") or cache.get("keyword", "影视")
        self.post_message(channel=channel, title="精确搜索",
                          text=f"🔍 正在获取「{title}」的资源…", userid=userid)
        if not self._configure_ok(channel, userid):
            return

        resources, msg = self._site_client.search_media_resources(media.get("path", ""))
        if not resources:
            self.post_message(channel=channel, title="无结果",
                              text=f"❌ {msg}", userid=userid)
            return

        ranked = sort_resources(resources, self._quality_weight, self._seeders_weight)
        self._cache[userid] = {
            "stage": "resource", "keyword": cache.get("keyword", ""),
            "media": media, "title": title,
            "resources": ranked, "page": 1, "timestamp": time.time(),
        }
        lines = self._format_resource_page(userid)
        self.post_message(channel=channel, title=f"资源列表 · {title[:24]}",
                          text="\n".join(lines), userid=userid)

    def _format_resource_page(self, userid: str) -> List[str]:
        """按当前页把资源格式化为带【全局序号】的列表（不触发转存）。"""
        cache = self._cache[userid]
        resources = cache["resources"]
        page = cache.get("page", 1)
        total = len(resources)
        page_size = self._page_size
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(page, total_pages))
        start = (page - 1) * page_size
        end = min(start + page_size, total)

        title = cache.get("title") or cache.get("keyword", "")
        lines = [
            f"🎬 「{title}」资源列表（共 {total} 条，第 {page}/{total_pages} 页）：", "",
        ]
        for idx in range(start, end):
            r = resources[idx]
            num = idx + 1  # 全局序号
            if r["type"] == "115":
                src = f"💾115 {r.get('quality_label') or ''}"
                extra = f"🔑 {r.get('receive_code') or '无码'}"
            else:
                src = f"🧲磁力 {r.get('quality_label') or ''} 做种{r.get('seeders', 0)}"
                extra = f"📦 {r.get('size') or '-'}"
            lines.append(f"【{num}】{src}")
            lines.append(f"     {extra}  {r.get('title', '')[:30]}")
        lines += ["", f"💡 回复【序号】转存；「上一页/下一页」翻页（当前 {page}/{total_pages}）；「返回」换影视。"]
        return lines

    def _handle_page(self, delta: int, channel, userid):
        """资源列表翻页。"""
        cache = self._cache.get(userid)
        if not cache or cache.get("stage") != "resource":
            self.post_message(channel=channel, title="提示",
                              text="请先选择一部影视。", userid=userid)
            return
        resources = cache.get("resources", [])
        total_pages = max(1, (len(resources) + self._page_size - 1) // self._page_size)
        new_page = max(1, min(cache.get("page", 1) + delta, total_pages))
        cache["page"] = new_page
        cache["timestamp"] = time.time()
        lines = self._format_resource_page(userid)
        self.post_message(channel=channel, title="资源列表（翻页）",
                          text="\n".join(lines), userid=userid)

    def _handle_pick(self, number: int, channel, userid):
        """第三级：按全局序号转存资源。"""
        cache = self._cache.get(userid)
        if not cache or cache.get("stage") != "resource":
            self.post_message(channel=channel, title="提示",
                              text="请先发送「片名？」并选择影视。", userid=userid)
            return
        resources = cache.get("resources", [])
        if not (1 <= number <= len(resources)):
            self.post_message(channel=channel, title="序号错误",
                              text=f"请输入 1-{len(resources)} 之间的序号。", userid=userid)
            return
        resource = resources[number - 1]
        kw = cache.get("title") or cache.get("keyword", "")
        if not self._pan115 or not self._115_cookie:
            self.post_message(channel=channel, title="资源链接",
                              text=f"💾 {resource['title']}\n🔗 {resource['url']}\n\n配置 115 Cookie 后可自动转存。",
                              userid=userid)
            return
        result = self._execute(resource, channel, userid, kw)
        self.post_message(channel=channel, title="转存结果", text=result, userid=userid)

    def _decide_plan(self, ranked: List[Dict]) -> Dict:
        """决策：115 优先；否则取排序最高的磁力。"""
        # 1) 优先 115 网盘资源
        shares = [r for r in ranked if r["type"] == "115" and r.get("share_code")]
        if shares:
            # 115 资源按画质排序，取最高画质
            shares = sort_resources(shares, self._quality_weight, 0)
            return {
                "resource": shares[0],
                "reason": f"发现 {len(shares)} 个115资源，优先转存画质最高的：{shares[0]['quality_label']} {shares[0]['title'][:30]}",
            }
        # 2) 无 115 → 最优磁力
        magnets = [r for r in ranked if r["type"] == "magnet"]
        if magnets:
            best = magnets[0]
            return {
                "resource": best,
                "reason": (f"无115资源，按画质(权重{self._quality_weight})+做种(权重{self._seeders_weight})"
                           f"选中磁力：{best['quality_label']} 做种{best['seeders']}"),
            }
        return {"resource": None, "reason": "未找到可转存的资源"}

    def _execute(self, resource: Optional[Dict], channel, userid, keyword: str = "") -> str:
        if not resource:
            return "❌ 无可转存资源"
        # 目标目录：自动分类到 115 网盘对应目录；关闭分类则存根目录
        target_cid = "0"
        classify_note = ""
        kw = keyword or self._cache.get(userid, {}).get("keyword", "")
        if self._auto_classify and self._pan115:
            media = identify_media(resource.get("title", ""), kw)
            rel = build_classify_path(media, self._classify_root, self._movie_dir, self._tv_dir)
            resolved = self._pan115.get_or_create_folder(rel, "0")
            if resolved:
                target_cid = resolved
                type_label = "电视剧" if media["media_type"] == "tv" else "电影"
                classify_note = f"\n📂 自动分类：{type_label} → {rel}"
            else:
                classify_note = f"\n⚠️ 分类目录「{rel}」创建失败，回退到根目录"

        if resource["type"] == "115":
            result = self._pan115.receive_share(resource["url"], target_cid)
        else:
            result = self._pan115.add_offline_task(resource["url"], target_cid)

        icon = "✅" if result.get("state") else "❌"
        return (
            f"{icon} {result['msg']}\n"
            f"📁 资源：{resource['title'][:50]}\n"
            f"💾 类型：{'115网盘转存' if resource['type'] == '115' else '115离线下载'}"
            f"{classify_note}"
        )
