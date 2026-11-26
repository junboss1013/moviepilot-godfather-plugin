"""
入库成功通知模块：教父资源转存到 115 网盘成功后，通知 MoviePilot「入库成功」。

转存到 115 网盘不经过 MoviePilot 本地整理流程，主程序不会自动感知。本模块主动广播
MoviePilot 原生的「整理完成」信号，让宿主链路（订阅完成判定 / 刮削 / 媒体库通知 /
历史记录）都能被触发，同时发一条标准的「整理入库」通知消息。

对外仅暴露 notify_library_success()，内部全部防御式 try/except：
任何一步失败都不影响转存主流程。兼容 MoviePilot V2 / V3。

合规说明：仅使用用户本人账号下的订阅与网盘资源。
"""
from typing import Any, Dict, List, Optional

try:
    from app.sdk.logging import logger
except (ImportError, ModuleNotFoundError):
    try:
        from app.log import logger
    except (ImportError, ModuleNotFoundError):
        import logging
        logger = logging.getLogger(__name__)


def _warn(msg: str):
    try:
        logger.warning(f"教父入库通知：{msg}")
    except Exception:
        pass


def _import_event():
    """返回 (eventmanager, EventType, NotificationType)；缺失项为 None。"""
    em = et = nt = None
    try:
        from app.sdk.events import eventmanager as _em
        em = _em
    except Exception:
        try:
            from app.core.event import eventmanager as _em
            em = _em
        except Exception:
            pass
    try:
        from app.schemas.types import EventType as _ET, NotificationType as _NT
        et, nt = _ET, _NT
    except Exception:
        pass
    return em, et, nt


def _build_file_item(path: str, name: str = "", pickcode: str = "", is_dir: bool = True):
    """构造 115 存储的 FileItem（storage='115'），失败返回 None。"""
    try:
        from app.schemas.file import FileItem
        return FileItem(
            storage="115",
            type="dir" if is_dir else "file",
            path=path or "/",
            name=name or None,
            pickcode=pickcode or None,
        )
    except Exception as e:
        _warn(f"构造 FileItem 失败: {e}")
        return None


def _build_transfer_info(target_item, need_scrape: bool = True):
    """构造整理结果 TransferInfo（success=True），失败返回 None。"""
    try:
        from app.schemas.transfer import TransferInfo
        return TransferInfo(
            success=True,
            target_item=target_item,
            target_diritem=target_item,
            transfer_type="教父115转存",
            file_count=1,
            need_scrape=need_scrape,
            need_notify=True,
        )
    except Exception as e:
        _warn(f"构造 TransferInfo 失败: {e}")
        return None


def _build_media_info(title: str, year: str = "", media_type: str = "",
                      season: Optional[int] = None):
    """尽力构造 MediaInfo（字幕/刮削链路需要）。失败返回 None，事件仍可发出。"""
    try:
        from app.media.meta import MetaInfo
        meta = MetaInfo(title)
    except Exception:
        meta = None
    mi = None
    for module in ("app.core.mediainfo", "app.media.mediainfo", "app.mediaserver.mediainfo"):
        try:
            mod = __import__(module, fromlist=["MediaInfo"])
            mi = mod.MediaInfo()
            break
        except Exception:
            continue
    if mi is None:
        return None, meta
    try:
        mi.title = title
        if year:
            mi.year = str(year)
        if season:
            mi.season = int(season)
        mi.type = _media_type_enum(media_type)
    except Exception:
        pass
    return mi, meta


def _media_type_enum(media_type: str):
    try:
        from app.schemas.types import MediaType
        tl = str(media_type or "").lower()
        if "movie" in tl or "电影" in str(media_type):
            return MediaType.MOVIE
        if "tv" in tl or "剧" in str(media_type):
            return MediaType.TV
    except Exception:
        pass
    return None


def _emit_transfer_complete(*, title: str, year: str = "", media_type: str = "",
                            season: Optional[int] = None, target_path: str = "",
                            pickcode: str = "") -> bool:
    """广播 TransferComplete 事件（原生「入库成功」信号）。成功返回 True。"""
    em, et, _ = _import_event()
    if em is None or et is None:
        _warn("无法加载 eventmanager/EventType，跳过 TransferComplete")
        return False
    try:
        target_item = _build_file_item(target_path, title, pickcode, is_dir=True)
        transferinfo = _build_transfer_info(target_item, need_scrape=True)
        mediainfo, meta = _build_media_info(title, year, media_type, season)
        payload: Dict[str, Any] = {
            "fileitem": target_item,
            "meta": meta,
            "mediainfo": mediainfo,
            "transferinfo": transferinfo,
            "downloader": "教父115转存",
            "download_hash": "",
        }
        em.send_event(et.TransferComplete, payload)
        try:
            logger.info(f"教父入库通知：已广播 TransferComplete「{title}」")
        except Exception:
            pass
        return True
    except Exception as e:
        _warn(f"广播 TransferComplete 失败: {e}")
        return False


def _send_organize_notice(plugin, *, title: str, text: str) -> bool:
    """发一条 MoviePilot 标准「整理入库」通知。"""
    _, _, nt = _import_event()
    try:
        if nt is not None and hasattr(nt, "Organize"):
            plugin.post_message(title=title, text=text, mtype=nt.Organize)
        else:
            plugin.post_message(title=title, text=text)
        return True
    except Exception as e:
        _warn(f"发整理入库通知失败: {e}")
        # 退回无 mtype 的最简通知
        try:
            plugin.post_message(title=title, text=text)
            return True
        except Exception:
            return False


def _finish_matched_subscribes(name: str, year: str = "", limit: int = 5) -> List[str]:
    """按片名匹配并结束 MoviePilot 订阅（资源转存插件用；防主程序重复下载）。
    返回被结束的订阅名列表。失败不影响主流程。"""
    done: List[str] = []
    try:
        from . import subscribe_helper as subh
    except Exception:
        # 资源转存插件目录可能没有该模块，尝试平级包导入
        try:
            from godfather_subscribe import subscribe_helper as subh  # type: ignore
        except Exception:
            return done
    try:
        subs = subh.list_subscribes()
    except Exception:
        return done
    key = subh.normalize(name)
    if not key:
        return done
    for sub in subs:
        if len(done) >= limit:
            break
        sname = subh.normalize(sub.get("name", ""))
        if not sname:
            continue
        matched = (sname == key) or (len(key) >= 4 and (key in sname or sname in key))
        if not matched:
            continue
        # 年份双向核对：两边都有且不相等则跳过，避免同名不同年份误伤
        sy = str(sub.get("year", "") or "").strip()
        if sy and year and sy[:4] != str(year)[:4]:
            continue
        sid = sub.get("id")
        if not sid:
            continue
        try:
            ok, _ = subh.finish_subscribe(int(sid))
            if ok:
                done.append(sub.get("name", ""))
        except Exception:
            continue
    return done


def notify_library_success(plugin, *, title: str, year: str = "", media_type: str = "",
                           season: Optional[int] = None, target_path: str = "",
                           pickcode: str = "", resource_title: str = "",
                           send_notice: bool = True, finish_sub: bool = True,
                           refresh_webhook: bool = True) -> Dict[str, Any]:
    """
    转存到 115 成功后统一通知 MoviePilot「入库成功」。

    :param plugin: 插件实例（用于 post_message）
    :param title: 媒体标题（片名/剧名）
    :param year: 年份
    :param media_type: 媒体类型（movie/tv 或 电影/电视剧）
    :param season: 季号（电视剧）
    :param target_path: 115 网盘内目标路径（作为入库路径）
    :param pickcode: 115 目标文件 pickcode（若有）
    :param resource_title: 转存的资源标题（通知正文展示）
    :param send_notice: 是否发「整理入库」通知
    :param finish_sub: 是否结束匹配的 MoviePilot 订阅
    :param refresh_webhook: 是否触发 strm-gen webhook 刷新 Emby 媒体库
    :return: 各步骤结果字典
    """
    result: Dict[str, Any] = {"transfer_event": False, "notice": False, "finished_subs": [], "webhook": False}
    result["transfer_event"] = _emit_transfer_complete(
        title=title, year=year, media_type=media_type, season=season,
        target_path=target_path, pickcode=pickcode)
    if send_notice:
        lines = [f"✅ 「{title}」已入库（教父站转存至 115 网盘）"]
        if resource_title:
            lines.append(f"📦 {str(resource_title)[:50]}")
        if target_path:
            lines.append(f"📁 {target_path}")
        if season:
            lines.append(f"📺 第 {int(season)} 季")
        result["notice"] = _send_organize_notice(
            plugin, title="整理入库", text="\n".join(lines))
    if finish_sub:
        try:
            result["finished_subs"] = _finish_matched_subscribes(title, year)
        except Exception as e:
            _warn(f"结束匹配订阅失败: {e}")
    # 触发 strm-gen webhook → Emby 媒体库立即刷新（独立于 MoviePilot 入库事件）
    if refresh_webhook:
        try:
            from .refresh_webhook import trigger_refresh
            result["webhook"] = trigger_refresh(plugin, path_hint=target_path or title)
        except Exception as e:
            _warn(f"触发刷新 webhook 失败: {e}")
    return result
