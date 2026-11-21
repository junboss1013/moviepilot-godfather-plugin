"""
教父订阅转存插件（MoviePilot V3）。

职责：定时遍历 MoviePilot 的订阅，用教父站优先找资源并转存到 115 网盘。
  - 教父站搜到资源 → 优先 115 转存（分享转存 / 磁力离线下载），自动按电影/电视剧分类；
  - 教父站搜不到 → 不干预订阅，交由 MoviePilot 主程序自身的订阅搜索（自动回退）；
  - 电影转存成功 → 结束该订阅，避免主程序重复下载；
  - 电视剧 → 按季去重并记录进度，保留订阅以便继续追更新季。

与「教父资源转存」（手动「片名？」搜索）是两个完全独立的插件，互不依赖。

合规说明：所有登录凭据（账号密码/Cookie）由用户自行提供，插件不破解鉴权、不撞库。
"""
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

try:
    from apscheduler.triggers.cron import CronTrigger
except Exception:  # 宿主应自带 apscheduler；缺失时定时服务降级为空
    CronTrigger = None

from .godfather_client import GodfatherClient
from .pan115_client import Pan115Client
from .resource_parser import sort_resources
from .media_identifier import identify_media, build_classify_path
from . import subscribe_helper as subh


class godfather_subscribe(_PluginBase):
    # ---------------- 插件元信息 ----------------
    plugin_name = "教父订阅转存"
    plugin_desc = "定时遍历MoviePilot订阅：教父站搜到就优先转存115，搜不到回退主程序。电影成功后结束订阅，电视剧按季去重继续追更。"
    plugin_icon = "godfather.png"
    plugin_version = "1.0.0"
    plugin_author = "WorkBuddy"
    author_url = "https://github.com"
    plugin_config_prefix = "godfather_subscribe_"
    plugin_order = 11
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

        self._sub_cron = "0 8,20 * * *"
        self._sub_mark_done = True
        self._sub_scope = "both"  # both/movie/tv
        self._sub_instant = False
        self._sub_progress: Dict[str, Any] = {}  # {订阅键: {'seasons': [1,2]}}
        self._sub_running = False  # 防止定时任务重入

        self._site_client: Optional[GodfatherClient] = None
        self._pan115: Optional[Pan115Client] = None

        # 自动分类（115 网盘内建目录，配合 rclone 挂载供 Emby 刮削）
        self._auto_classify = True
        self._classify_root = "影视库"
        self._movie_dir = "电影"
        self._tv_dir = "电视剧"

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

        self._sub_cron = (config.get("sub_cron") or "0 8,20 * * *").strip()
        self._sub_mark_done = bool(config.get("sub_mark_done", True))
        self._sub_scope = (config.get("sub_scope") or "both").strip()
        self._sub_instant = bool(config.get("sub_instant", False))
        self._auto_classify = bool(config.get("auto_classify", True))
        self._classify_root = (config.get("classify_root") or "影视库").strip()
        self._movie_dir = (config.get("movie_dir") or "电影").strip()
        self._tv_dir = (config.get("tv_dir") or "电视剧").strip()
        self._load_sub_progress()

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
                logger.error(f"教父订阅插件：教父站客户端初始化失败: {e}")
            try:
                self._pan115 = Pan115Client(cookie=self._115_cookie)
            except Exception as e:
                logger.error(f"教父订阅插件：115 客户端初始化失败: {e}")
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

    # ---------------- 标准接口 ----------------
    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_page(self) -> List[dict]:
        return []

    # ---------------- 定时服务 ----------------
    def get_service(self) -> List[Dict[str, Any]]:
        """按 cron 定时遍历订阅，教父站优先转存。服务 ID 稳定唯一。"""
        if not (self._enabled and CronTrigger is not None):
            return []
        try:
            trigger = CronTrigger.from_crontab(self._sub_cron)
        except Exception as e:
            logger.error(f"教父订阅插件：cron 表达式无效「{self._sub_cron}」: {e}")
            return []
        return [{
            "id": "godfather_subscribe.SubscribeSync",
            "name": "教父订阅转存（定时遍历订阅）",
            "trigger": trigger,
            "func": self.run_subscribe_sync,
            "kwargs": {},
        }]

    # ---------------- 进度持久化（电视剧按季去重） ----------------
    def _load_sub_progress(self):
        self._sub_progress = {}
        try:
            data = self.get_data("gf_sub_progress")
            if data is not None and not hasattr(data, "__await__") and isinstance(data, dict):
                self._sub_progress = data
        except Exception:
            pass

    def _save_sub_progress(self):
        try:
            self.save_data("gf_sub_progress", self._sub_progress)
        except Exception:
            pass

    # ---------------- 订阅联动核心 ----------------
    @eventmanager.register(EventType.SubscribeAdded)
    def on_subscribe_added(self, event: Event):
        """新订阅添加时可选立即尝试一次（默认关闭，避免与主程序初始搜索竞争）。"""
        if not (self._enabled and self._sub_instant):
            return
        data = (event.event_data or {}) if event else {}
        name = str(data.get("name") or data.get("title") or "")
        if name:
            self._process_one_by_name(name)

    def run_subscribe_sync(self):
        """定时入口：遍历订阅，教父站优先转存，搜不到则回退主程序。"""
        if not self._enabled:
            return
        if self._sub_running:
            logger.info("教父订阅插件：上一轮仍在运行，跳过本次")
            return
        self._sub_running = True
        try:
            subscribes = subh.list_subscribes()
            if not subscribes:
                logger.info("教父订阅插件：未读取到订阅（或无权限访问订阅数据）")
                return
            logger.info(f"教父订阅插件：开始遍历 {len(subscribes)} 条订阅")
            done_cnt = fail_cnt = skip_cnt = 0
            for sub in subscribes:
                if not self._scope_allowed(sub):
                    continue
                ok = self._process_subscribe(sub)
                if ok is True:
                    done_cnt += 1
                elif ok is False:
                    fail_cnt += 1
                else:
                    skip_cnt += 1  # 无结果/跳过，回退主程序
            logger.info(f"教父订阅插件：完成 {done_cnt} 条转存，{fail_cnt} 条失败，"
                        f"{skip_cnt} 条回退主程序")
        except Exception as e:
            logger.error(f"教父订阅插件：定时遍历异常: {e}")
        finally:
            self._sub_running = False

    def _scope_allowed(self, sub: Dict[str, Any]) -> bool:
        if self._sub_scope == "movie":
            return subh.is_movie(sub)
        if self._sub_scope == "tv":
            return subh.is_tv(sub)
        return True

    def _process_one_by_name(self, name: str):
        """按片名处理单条（订阅新增时即时触发的轻量路径）。"""
        if not self._site_client or not self._pan115 or not self._115_cookie:
            return
        sub = {"id": 0, "name": name, "year": "", "keyword": name,
               "type": "", "season": None, "state": "R"}
        self._process_subscribe(sub)

    def _process_subscribe(self, sub: Dict[str, Any]) -> Optional[bool]:
        """
        处理单条订阅。
        返回 True=已转存成功, False=有资源但转存失败, None=教父站无结果/跳过(回退主程序)。
        """
        name = sub.get("name") or ""
        if not name:
            return None
        if not self._site_client or not self._pan115 or not self._115_cookie:
            logger.warning("教父订阅插件：教父站或115客户端未就绪，跳过")
            return None

        # 电视剧：该季已转存过则跳过（按季去重，继续追更）
        season = sub.get("season")
        if subh.is_tv(sub) and season and subh.season_done(self._sub_progress, sub, int(season)):
            logger.info(f"教父订阅插件：「{name}」S{int(season):02d} 已转存过，跳过")
            return None

        # 教父站搜索
        try:
            resources, msg = self._site_client.search(name)
        except Exception as e:
            logger.error(f"教父订阅插件：搜索「{name}」异常: {e}")
            return None
        if not resources:
            logger.info(f"教父订阅插件：「{name}」教父站无结果 → 回退主程序（{msg}）")
            return None

        # 片名+年份校验，避免张冠李戴；无严格匹配时退回全部由排序择优
        matched = [r for r in resources if subh.resource_matches(r, sub)]
        pool = matched or resources
        ranked = sort_resources(pool, self._quality_weight, self._seeders_weight)
        plan = self._decide_plan(ranked)
        resource = plan.get("resource")
        if not resource:
            logger.info(f"教父订阅插件：「{name}」无可转存资源 → 回退主程序")
            return None

        # 识别媒体并生成分类路径
        media = identify_media(resource.get("title", ""), name)
        if subh.is_tv(sub) and season:
            media["media_type"] = "tv"
            media["season"] = int(season)
        rel = build_classify_path(media, self._classify_root, self._movie_dir, self._tv_dir)
        target_cid = "0"
        if self._auto_classify and self._pan115:
            resolved = self._pan115.get_or_create_folder(rel, "0")
            if resolved:
                target_cid = resolved

        # 执行转存
        if resource["type"] == "115":
            result = self._pan115.receive_share(resource["url"], target_cid)
        else:
            result = self._pan115.add_offline_task(resource["url"], target_cid)
        if not bool(result.get("state")):
            logger.warning(f"教父订阅插件：「{name}」转存失败: {result.get('msg')}")
            return False

        # 成功：电视剧记季进度；电影按需结束订阅
        if subh.is_tv(sub) and media.get("season"):
            subh.mark_season_done(self._sub_progress, sub, int(media["season"]))
            self._save_sub_progress()
        if subh.is_movie(sub) and self._sub_mark_done and sub.get("id"):
            d_ok, d_msg = subh.finish_subscribe(int(sub["id"]))
            logger.info(f"教父订阅插件：「{name}」结束订阅 {d_msg}")

        # 通知
        try:
            self.post_message(
                title="教父订阅转存",
                text=(f"✅ 订阅「{name}」已由教父站优先转存115\n"
                      f"📁 {rel}\n📦 {resource.get('title', '')[:50]}"),
            )
        except Exception:
            pass
        logger.info(f"教父订阅插件：「{name}」转存成功（{rel}）")
        return True

    def _decide_plan(self, ranked: List[Dict]) -> Dict:
        """决策：115 优先；否则取排序最高的磁力。"""
        shares = [r for r in ranked if r["type"] == "115" and r.get("share_code")]
        if shares:
            shares = sort_resources(shares, self._quality_weight, 0)
            return {"resource": shares[0],
                    "reason": f"115资源优先：{shares[0]['quality_label']}"}
        magnets = [r for r in ranked if r["type"] == "magnet"]
        if magnets:
            return {"resource": magnets[0],
                    "reason": f"最优磁力：{magnets[0]['quality_label']} 做种{magnets[0]['seeders']}"}
        return {"resource": None, "reason": "未找到可转存的资源"}

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
                                "text": "【教父订阅转存】定时遍历 MoviePilot 订阅：教父站搜到就优先转存115；搜不到则不动订阅、由主程序自行搜索（自动回退）。电影转存成功后结束订阅以免重复下载；电视剧按季去重并保留订阅以继续追更。需先配置教父站账号密码与115 Cookie。",
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
                                "hint": "站点有人机验证时用内置浏览器自动完成（需 playwright+chromium）",
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
                                "placeholder": "UID=...; CID=...; SEID=...; KID=...",
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
                                "model": "sub_cron", "label": "定时 Cron(每天2次)",
                                "placeholder": "0 8,20 * * *",
                                "hint": "5段cron：分 时 日 月 周；默认每天8点、20点各一次",
                                "persistent-hint": True, "clearable": True,
                            },
                        }],
                    }, {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [{
                            "component": "VSelect",
                            "props": {
                                "model": "sub_scope", "label": "订阅类型范围",
                                "items": ["both", "movie", "tv"],
                                "hint": "both=电影+电视剧, movie=仅电影, tv=仅电视剧",
                                "persistent-hint": True,
                            },
                        }],
                    }, {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [{
                            "component": "VSwitch",
                            "props": {
                                "model": "sub_mark_done", "label": "电影成功后结束订阅",
                                "hint": "防止主程序重复下载；关闭则保留订阅",
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
                                "model": "sub_instant", "label": "订阅新增时立即尝试",
                                "hint": "关闭则只按 cron 定时执行", "persistent-hint": True,
                            },
                        }],
                    }, {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [{
                            "component": "VSwitch",
                            "props": {
                                "model": "auto_classify", "label": "转存时自动分类(电影/电视剧)",
                                "hint": "在115网盘自动建 影视库/电影/片名(年份)、影视库/电视剧/片名/Season XX",
                                "persistent-hint": True,
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
                                "model": "classify_root", "label": "分类根目录(115网盘内)",
                                "placeholder": "影视库", "clearable": True,
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
        }
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
            "sub_cron": "0 8,20 * * *",
            "sub_mark_done": True,
            "sub_scope": "both",
            "sub_instant": False,
            "auto_classify": True,
            "classify_root": "影视库",
            "movie_dir": "电影",
            "tv_dir": "电视剧",
        }
