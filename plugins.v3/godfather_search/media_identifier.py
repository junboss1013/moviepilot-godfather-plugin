"""
媒体信息识别与 115 网盘分类路径生成。

从「资源标题」和「用户搜索关键词」判断媒体类型（电影/电视剧），
提取片名、年份、季号，生成用于 115 网盘的分类目录路径。
配合 rclone / openlist 把 115 挂载成本地目录后，Emby 即可按
「电影」「电视剧」两个根目录分别扫描刮削。

设计要点：
  - 纯标准库实现，不依赖 MoviePilot，可独立单元测试；
  - 片名优先取用户搜索关键词（往往就是准确片名），标题作为回退；
  - 类型识别以标题特征为准（S01E01 / 第x集 / 全x集 / Season / 第x季）。

合规说明：仅做标题文本解析，不涉及任何内容获取或鉴权绕过。
"""
import re
from typing import Any, Dict, Optional

# ---------------------------------------------------------------- 电视剧特征
_TV_HINTS = [
    re.compile(r"S\s*\d{1,2}\s*E\s*\d{1,3}", re.I),        # S01E01 / S1E1
    re.compile(r"Season\s*[.\s_-]*\d{1,2}", re.I),          # Season 1 / Season.2
    re.compile(r"第\s*[0-9一二三四五六七八九十百]+\s*季"),   # 第一季 / 第2季
    re.compile(r"第\s*\d{1,3}\s*集"),                        # 第01集
    re.compile(r"全\s*\d{1,3}\s*集"),                        # 全24集
    re.compile(r"\bE\s*\d{2,3}\b", re.I),                    # E01（单集）
]

# 年份：限定 19xx / 20xx（到 2029），避免误匹配 2160p / 1080p
_YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20[0-2]\d)(?!\d)")

# 画质/技术/字幕组等噪音词，用于从标题切出干净片名
_NOISE_RE = re.compile(
    r"(?:1080p|2160p|4k|720p|480p|bluray|blu[\s._-]?ray|bdrip|brrip|remux|"
    r"web[\s._-]?dl|hdtv|hdr|10bit|x264|x265|h264|h265|hevc|aac|ac3|dts|"
    r"truehd|atmos|中字|中文字幕|简繁|繁体|国语|粤语|双语|字幕组|原盘|"
    r"bdmv|complete|全集|[a-z]{2,5}sub)",
    re.I,
)

_CN_NUM = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _is_tv(text: str) -> bool:
    return any(p.search(text) for p in _TV_HINTS)


def _extract_year(text: str) -> str:
    found = _YEAR_RE.findall(text)
    return found[-1] if found else ""


def _extract_season(text: str) -> Optional[int]:
    m = re.search(r"S\s*(\d{1,2})\s*E\s*\d{1,3}", text, re.I)
    if not m:
        m = re.search(r"Season\s*[.\s_-]*(\d{1,2})", text, re.I)
    if not m:
        m = re.search(r"第\s*([0-9一二三四五六七八九十百]+)\s*季", text)
        if m:
            raw = m.group(1)
            if raw in _CN_NUM:
                return _CN_NUM[raw]
            return int(raw) if raw.isdigit() else None
    return int(m.group(1)) if m else None


def _clean_name(raw: str) -> str:
    s = (raw or "").strip()
    # 去掉 [字幕组] (年份) 【标签】 等括号内容
    s = re.sub(r"[\[【(（][^\]】)）]{0,40}[\]】)）]", " ", s)
    s = _NOISE_RE.sub(" ", s)
    s = _YEAR_RE.sub(" ", s)
    s = re.sub(r"[._+]+", " ", s)
    # 去掉文件名非法字符
    s = re.sub(r'[\\/:*?"<>|]', "", s)
    s = re.sub(r"\s+", " ", s).strip(" -–—·")
    return s


def identify_media(title: str = "", keyword: str = "") -> Dict[str, Any]:
    """
    识别媒体信息。
    :param title: 资源标题（教父站 t 字段）
    :param keyword: 用户搜索关键词（片名优先来源）
    :return: {media_type: movie|tv, name, year, season}
    """
    t = (title or "").strip()
    kw = (keyword or "").strip()
    is_tv = _is_tv(t)
    year = _extract_year(t)
    season = _extract_season(t) if is_tv else None
    name = _clean_name(kw) or _clean_name(t) or "未分类影视"
    return {
        "media_type": "tv" if is_tv else "movie",
        "name": name,
        "year": year,
        "season": season,
    }


def build_classify_path(
    media: Dict[str, Any],
    root: str = "影视库",
    movie_dir: str = "电影",
    tv_dir: str = "电视剧",
) -> str:
    """
    生成 115 网盘内的分类目录路径（相对根目录）。
    电影: 影视库/电影/片名 (年份)
    电视剧: 影视库/电视剧/片名/Season 01
    """
    name = media.get("name") or "未分类影视"
    root = (root or "").strip().strip("/")
    movie_dir = (movie_dir or "").strip().strip("/") or "电影"
    tv_dir = (tv_dir or "").strip().strip("/") or "电视剧"

    if media.get("media_type") == "tv":
        parts = [root, tv_dir, name]
        season = media.get("season")
        if season:
            parts.append("Season %02d" % int(season))
    else:
        year = media.get("year") or ""
        folder = f"{name} ({year})" if year else name
        parts = [root, movie_dir, folder]
    return "/".join(p for p in parts if p)


if __name__ == "__main__":
    demos = [
        ("肖申克的救赎.The.Shawshank.Redemption.1994.1080p.BluRay.x265", "肖申克的救赎"),
        ("权力的游戏.Game.of.Thrones.S01E01.1080p.中字", "权力的游戏"),
        ("诡秘之主.第01集.1080p", "诡秘之主"),
        ("阿凡达.Avatar.2009.4K.HDR.REMUX", "阿凡达"),
        ("怪奇物语.Season.2.全09集.720p", "怪奇物语"),
        ("繁花.2023.全集.4K", "繁花"),
    ]
    for title, kw in demos:
        m = identify_media(title, kw)
        print(f"{m['media_type']:5} | {build_classify_path(m)}")
