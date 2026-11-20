"""
教父资源转存插件（MoviePilot V2）。

触发：在任意消息界面发送「影片名？」（以中文/英文问号结尾）。
流程：
  1. 用账号密码自动登录教父站（兼容 Cookie）搜索影视资源；
  2. 优先识别 115 网盘资源 → 直接调用 115 转存接口存到网盘；
  3. 若无 115 资源 → 识别磁力资源 → 按「画质 + 做种数」加权排序 → 取最优
     磁力，用 115 离线下载转存到网盘；
  4. 通过消息返回决策依据与执行结果。

合规说明：所有登录凭据（账号密码/Cookie）由用户自行提供，插件不破解鉴权、不撞库。
"""
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType

from .godfather_client import GodfatherClient
from .pan115_client import Pan115Client
from .resource_parser import sort_resources


class godfather_search(_PluginBase):
    # ---------------- 插件元信息 ----------------
    plugin_name = "教父资源转存"
    plugin_desc = "输入「片名？」搜索教父站：优先转存115网盘资源，否则按画质+做种数选最优磁力用115离线下载。"
    plugin_icon = "godfather.png"
    plugin_version = "1.2.1"
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
        self._target_cid = "0"
        self._target_path = ""
        self._quality_weight = 1.0
        self._seeders_weight = 1.0
        self._max_results = 8
        self._trigger_suffix = "？"
        self._auto_transfer = True

        self._site_client: Optional[GodfatherClient] = None
        self._pan115: Optional[Pan115Client] = None
        # 用户会话缓存：{userid: {'resources': [...], 'keyword': str, 'timestamp': float}}
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
        self._target_cid = str(config.get("target_cid") or "0")
        self._target_path = config.get("target_path", "").strip()
        self._quality_weight = float(config.get("quality_weight", 1.0) or 1.0)
        self._seeders_weight = float(config.get("seeders_weight", 1.0) or 1.0)
        self._max_results = int(config.get("max_results", 8) or 8)
        self._trigger_suffix = config.get("trigger_suffix") or "？"
        self._auto_transfer = bool(config.get("auto_transfer", True))

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
                                    "text": "发送「影片名？」触发搜索。优先转存115资源；无115时按画质+做种数选最优磁力，用115离线下载。请填写教父站账号密码，插件会自动登录（也兼容 Cookie）。",
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
                                    "model": "target_cid", "label": "115 目标目录 ID",
                                    "placeholder": "0", "hint": "0 表示根目录",
                                    "persistent-hint": True,
                                },
                            }],
                        }, {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [{
                                "component": "VTextField",
                                "props": {
                                    "model": "target_path", "label": "或按路径转存",
                                    "placeholder": "媒体库/电影",
                                    "hint": "填了路径则优先按路径查找/创建目录，忽略上面的 ID",
                                    "persistent-hint": True, "clearable": True,
                                },
                            }],
                        }, {
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
            "target_cid": "0",
            "target_path": "",
            "quality_weight": 1.0,
            "seeders_weight": 1.0,
            "max_results": 8,
            "trigger_suffix": "？",
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

        # 触发词：以配置的问号结尾（同时兼容英文 ?）
        clean = text.rstrip("？?").strip()
        if not (text.endswith("？") or text.endswith("?")):
            return
        if not clean:
            return

        # 纯数字：选择上一条结果中的某个资源
        if clean.isdigit():
            self._handle_pick(int(clean), channel, userid)
            return

        # 关键词搜索
        self._handle_search(clean, channel, userid)

    # ---------------- 核心流程 ----------------
    def _handle_search(self, keyword: str, channel, userid):
        self.post_message(channel=channel, title="教父搜索",
                          text=f"🔍 正在搜索「{keyword}」...", userid=userid)

        if not self._site_client:
            self.post_message(channel=channel, title="配置错误",
                              text="教父站客户端未初始化，请检查是否启用插件。", userid=userid)
            return
        if not (self._site_username and self._site_password) and not self._site_cookie:
            self.post_message(channel=channel, title="缺少登录凭据",
                              text="请先配置教父站账号和密码（或旧版 Cookie）再搜索。", userid=userid)
            return

        resources, msg = self._site_client.search(keyword)
        if not resources:
            self.post_message(channel=channel, title="无结果",
                              text=f"❌ {msg}", userid=userid)
            return

        # 缓存结果
        self._cache[userid] = {
            "keyword": keyword, "resources": resources, "timestamp": time.time(),
        }

        # 排序后的资源
        ranked = sort_resources(resources, self._quality_weight, self._seeders_weight)
        top = ranked[:self._max_results]

        lines = [f"🎬 「{keyword}」找到 {len(resources)} 条资源（展示前 {len(top)}）:", ""]
        for i, r in enumerate(top, 1):
            if r["type"] == "115":
                lines.append(
                    f"【{i}】💾115 {r['quality_label']} {r['title'][:46]}\n"
                    f"    🔑 {r.get('receive_code') or '无码'}  📦 {r.get('size') or '-'}"
                )
            else:
                lines.append(
                    f"【{i}】🧲磁力 {r['quality_label']} 做种{r['seeders']} {r['title'][:42]}\n"
                    f"    📦 {r.get('size') or '-'}"
                )

        # 决策：优先 115，否则最优磁力
        if not self._pan115 or not self._115_cookie:
            lines += ["", "⚠️ 未配置 115 Cookie，无法自动转存。", "💡 回复编号可查看该资源链接。"]
        elif not self._auto_transfer:
            lines += ["", "💡 自动转存已关闭，回复编号执行转存。"]
        else:
            plan = self._decide_plan(ranked)
            lines += ["", "─" * 12, f"🎯 自动决策：{plan['reason']}"]
            result = self._execute(plan["resource"], channel, userid)
            lines.append(result)

        lines += ["", "💡 回复编号（如 2？）可转存指定资源。"]
        self.post_message(channel=channel, title="教父资源结果",
                          text="\n".join(lines), userid=userid)

    def _handle_pick(self, number: int, channel, userid):
        cache = self._cache.get(userid)
        if not cache or time.time() - cache["timestamp"] > 1800:
            self.post_message(channel=channel, title="提示",
                              text="结果已过期，请重新搜索。", userid=userid)
            return
        ranked = sort_resources(cache["resources"], self._quality_weight, self._seeders_weight)
        top = ranked[:self._max_results]
        if not (1 <= number <= len(top)):
            self.post_message(channel=channel, title="编号错误",
                              text=f"请输入 1-{len(top)} 之间的编号。", userid=userid)
            return
        resource = top[number - 1]
        if not self._pan115 or not self._115_cookie:
            self.post_message(channel=channel, title="资源链接",
                              text=f"💾 {resource['title']}\n🔗 {resource['url']}\n\n配置 115 Cookie 后可自动转存。",
                              userid=userid)
            return
        result = self._execute(resource, channel, userid)
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

    def _execute(self, resource: Optional[Dict], channel, userid) -> str:
        if not resource:
            return "❌ 无可转存资源"
        # 目标目录：路径优先
        target_cid = self._target_cid or "0"
        if self._target_path:
            resolved = self._pan115.get_or_create_folder(self._target_path, target_cid)
            if resolved:
                target_cid = resolved
            else:
                return "❌ 115 目标目录创建/查找失败"

        if resource["type"] == "115":
            result = self._pan115.receive_share(resource["url"], target_cid)
        else:
            result = self._pan115.add_offline_task(resource["url"], target_cid)

        icon = "✅" if result.get("state") else "❌"
        return (
            f"{icon} {result['msg']}\n"
            f"📁 资源：{resource['title'][:50]}\n"
            f"💾 类型：{'115网盘转存' if resource['type'] == '115' else '115离线下载'}"
        )
