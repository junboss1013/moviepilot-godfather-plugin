"""
订阅联动辅助模块：读取 MoviePilot 订阅、结束订阅、按季去重、资源匹配。

职责：
  1. 读取宿主订阅列表（兼容 V2/V3 的 SubscribeOper 入口）；
  2. 结束订阅（电影转存成功后调用，避免主程序再次搜索并重复下载）；
  3. 按季进度去重（电视剧追更，避免重复转存已入库的季）；
  4. 资源与订阅的匹配校验（片名 + 年份双向核对，防止张冠李戴）。

设计要点：
  - 所有宿主 API 调用都做防御式 try/except，任何失败都不影响插件主流程；
  - 归一化、匹配、去重等纯逻辑不依赖 MoviePilot，可独立测试；
  - V2/V3 导入兼容。

合规说明：仅读取/结束用户自己账号下的订阅与网盘资源，不涉及鉴权绕过。
"""
from typing import Any, Dict, List, Optional, Tuple

try:
    from app.sdk.logging import logger
except (ImportError, ModuleNotFoundError):
    try:
        from app.log import logger
    except (ImportError, ModuleNotFoundError):
        import logging
        logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- 宿主数据访问
def get_subscribe_oper():
    """获取订阅操作对象，兼容 V2/V3；失败返回 None。"""
    try:
        from app.db.oper.subscribe import SubscribeOper
        return SubscribeOper()
    except Exception as e:
        _warn(f"无法加载 SubscribeOper: {e}")
        return None


def _warn(msg: str):
    try:
        logger.warning(f"教父订阅插件：{msg}")
    except Exception:
        pass


def subscribe_to_dict(sub: Any) -> Dict[str, Any]:
    """把订阅 ORM 对象转成普通字典，兼容各版本字段差异。"""
    def g(*names, default=""):
        for n in names:
            v = getattr(sub, n, None)
            if v is not None:
                return v
        return default
    return {
        "id": g("id", "sid", default=0),
        "name": str(g("name", "title", default="") or ""),
        "year": str(g("year", default="") or ""),
        "type": str(g("type", "media_type", default="") or ""),
        "season": g("season", default=None),
        "keyword": str(g("keyword", default="") or ""),
        "state": str(g("state", default="") or ""),
    }


def list_subscribes() -> List[Dict[str, Any]]:
    """列出订阅（仅必要字段的字典）。失败返回空列表。"""
    oper = get_subscribe_oper()
    if oper is None:
        return []
    rows: List[Any] = []
    try:
        rows = oper.list() or []
    except Exception:
        try:
            rows = oper.get_statistics() or []
        except Exception as e:
            _warn(f"读取订阅列表失败: {e}")
            return []
    result = []
    for sub in rows:
        d = subscribe_to_dict(sub)
        if d.get("name"):
            result.append(d)
    return result


def finish_subscribe(sid: int) -> Tuple[bool, str]:
    """
    结束订阅（等同于"完成"）：从订阅表移除，避免主程序继续搜索并重复下载。
    MoviePilot 的订阅完成在数据库层是「移出订阅表 + 写历史」；插件只通过
    公开的 SubscribeOper 结束订阅，不直接操作 ORM。
    """
    if not sid:
        return False, "缺少订阅 ID"
    oper = get_subscribe_oper()
    if oper is None:
        return False, "无法访问订阅数据"
    last = "无可用方法"
    for method in ("delete", "delete_by_id", "remove", "complete"):
        fn = getattr(oper, method, None)
        if not callable(fn):
            continue
        try:
            fn(sid)
            return True, f"已结束订阅 #{sid}"
        except Exception as e:
            last = str(e)
    return False, f"结束订阅失败: {last}"


# ---------------------------------------------------------------- 纯逻辑（可测试）
def normalize(s: str) -> str:
    """片名归一化：去空格/标点/大小写，便于双向包含匹配。"""
    import re
    s = (s or "").lower()
    s = re.sub(r"[\s._\-+（）()\[\]【】'\"，,·:：!！?？|/\\]", "", s)
    return s


def resource_matches(resource: Dict, sub: Dict) -> bool:
    """资源标题是否与订阅匹配：片名双向包含 + 年份核对。"""
    title_raw = str(resource.get("title", "") or "")
    title = normalize(title_raw)
    name = normalize(sub.get("name", ""))
    keyword = normalize(sub.get("keyword", ""))
    if not title:
        return False
    for token in filter(None, (name, keyword)):
        if token in title or (len(token) >= 6 and title[:len(token)] in token):
            return True
    year = str(sub.get("year", "") or "")
    if year and year in title_raw and len(name) >= 4 and name[:6] in title:
        return True
    return False


def progress_key(sub: Dict) -> str:
    """订阅在进度表中的稳定键。"""
    return str(sub.get("id") or normalize(sub.get("name", "")) or "unknown")


def season_done(progress: Dict, sub: Dict, season: int) -> bool:
    """某季是否已转存过。"""
    seasons = (progress.get(progress_key(sub)) or {}).get("seasons") or []
    return int(season) in [int(x) for x in seasons if str(x).isdigit()]


def mark_season_done(progress: Dict, sub: Dict, season: int) -> Dict:
    """记录某季已转存（就地修改并返回 progress）。"""
    entry = progress.setdefault(progress_key(sub), {"seasons": []})
    seasons = entry.setdefault("seasons", [])
    done = [int(x) for x in seasons if str(x).isdigit()]
    if int(season) not in done:
        seasons.append(int(season))
    return progress


def is_movie(sub: Dict) -> bool:
    t = str(sub.get("type", "") or "")
    tl = t.lower()
    return ("电影" in t) or ("movie" in tl) or tl in ("mov", "m")


def is_tv(sub: Dict) -> bool:
    t = str(sub.get("type", "") or "")
    tl = t.lower()
    return ("剧" in t) or ("tv" in tl) or ("show" in tl)
