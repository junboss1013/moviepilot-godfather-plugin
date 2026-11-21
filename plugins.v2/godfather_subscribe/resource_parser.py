"""
教父站资源解析器（适配站点真实结构 + 兼容旧版 HTML）。

站点真实结构（2026-11 实测）：
  搜索页 /search?q=<词> 只列影片，链接到 /mv/xxxx；
  详情页数据来自 JSON 接口 GET /res/downurl/mv/xxxx，其 downlist.list 为「列式数组」：
    m: btih hash 列表        t: 标题列表
    z: 中字标记列表          s: 大小列表（如 "7.37G"）
    e: 做种数列表            p: 画质代码列表（i14/i19/i8/...，对应 type.b）
    u: 短链 code             k: 类型标记            n: 发布时间
  downlist.type 给出画质映射：type.a[i] <-> type.b[i]，如 i8 -> 1080P。
  资源本质均为磁力；网盘分类走 keepshare 离线中转，站内无 115 分享直链。

职责：
  1. parse_downlist_json(): 从上述 JSON 解析资源条目（主路径）；
  2. parse_resources(): 从 HTML 解析（兜底/兼容）；
  3. parse_quality()/sort_resources(): 画质与做种加权排序。

本模块不依赖 MoviePilot，可独立单元测试。
"""
import html as html_lib
import math
import re
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

from bs4 import BeautifulSoup


# ---------------------------------------------------------------- 链接正则（HTML 兜底用）
RE_115 = re.compile(
    r"https?://(?:[\w-]+\.)*115(?:cdn|vod)?\.com/s/[A-Za-z0-9_-]{4,}[^\s<>\"'`\]]*",
    re.I,
)
RE_MAGNET = re.compile(r"magnet:\?xt=urn:btih:[A-Za-z0-9]{32,}[^\s<>\"'`\]]*", re.I)


# ---------------------------------------------------------------- 画质代码映射
# 站点 type.b -> type.a 画质档位；给出 (分数, 标签)
QUALITY_CODE_MAP: Dict[str, Tuple[int, str]] = {
    "i14": (100, "4K"),        # 4K
    "i19": (104, "中字4K"),     # 中字4K（中字小幅加分）
    "i8": (80, "1080P"),       # 1080P
    "i16": (84, "中字1080P"),   # 中字1080P
    "i2": (60, "720P"),        # 720P
    "i17": (64, "中字720P"),    # 中字720P
    "i6": (92, "原盘"),         # 原盘
    "i4": (20, "未知"),         # 未知
}
# 站点 type.a 画质名 -> (分数, 标签)（当只拿到中文档位名时）
QUALITY_NAME_MAP: Dict[str, Tuple[int, str]] = {
    "4k": (100, "4K"), "中字4k": (104, "中字4K"),
    "1080p": (80, "1080P"), "中字1080p": (84, "中字1080P"),
    "720p": (60, "720P"), "中字720p": (64, "中字720P"),
    "原盘": (92, "原盘"), "未知": (20, "未知"),
}


def _quality_from_code(code: str) -> Tuple[int, str]:
    return QUALITY_CODE_MAP.get((code or "").strip().lower(), (20, "未知"))


def parse_quality(text: str) -> Tuple[int, str]:
    """从标题文本识别画质，返回 (画质分数, 画质标签)。分数越高画质越好。"""
    t = (text or "").lower()
    score, label = 20, "未知"

    if re.search(r"(4k|uhd|2160p|2160i)", t):
        score, label = 100, "4K"
    elif re.search(r"(原盘|bluray|blu[\s-]?ray|bdremux|remux|bdrip|bdmv)", t):
        score, label = 92, "原盘"
    elif re.search(r"(1080p|fhd|full[\s-]?hd|蓝光)", t):
        score, label = 80, "1080P"
    elif re.search(r"(720p|hd[\s.-]?tv|hdtv)", t):
        score, label = 60, "720P"
    elif re.search(r"(480p|576p|\bsd\b|dvd|vcd)", t):
        score, label = 30, "标清"

    bonus = 0
    if re.search(r"(中字|chs|gb|简繁|繁体)", t):
        bonus += 4
    if re.search(r"remux", t):
        bonus += 8
    if re.search(r"(x265|h[\s.]?265|hevc|10bit|10[\s-]bit)", t):
        bonus += 6
    if re.search(r"(hdr|dolby[\s-]?vision|\bdv\b|hdr10)", t):
        bonus += 4
    if re.search(r"(atmos|truehd|dts|dd5\.1|5\.1ch)", t):
        bonus += 3
    return score + bonus, label


# ---------------------------------------------------------------- 做种数识别
_SEED_PATTERNS = [
    r"做种\s*[:：]?\s*(\d+)", r"(\d+)\s*个?做种",
    r"种子(?:数)?\s*[:：]?\s*(\d+)", r"seeders?\s*[:：]?\s*(\d+)",
    r"(\d+)\s*seeders?", r"热度\s*[:：]?\s*(\d+)", r"\bS\s*[:：]\s*(\d+)",
]


def parse_seeders(text: str) -> int:
    t = text or ""
    for pattern in _SEED_PATTERNS:
        m = re.search(pattern, t, re.I)
        if m:
            try:
                return int(m.group(1))
            except (ValueError, IndexError):
                continue
    return 0


# ---------------------------------------------------------------- JSON 主解析器
def parse_downlist_json(data: Any) -> List[Dict[str, Any]]:
    """
    解析详情页 /res/downurl 接口返回的 JSON（列式数组）。
    每列（m/t/e/z/s/p/n）长度相同，按下标对应一条资源。
    """
    if isinstance(data, str):
        try:
            import json as _json
            data = _json.loads(data)
        except Exception:
            return []
    if not isinstance(data, dict):
        return []

    dl = data.get("downlist") or {}
    L = dl.get("list") or {}
    if not isinstance(L, dict):
        return []

    # 站点画质映射：type.a 名称 <-> type.b 代码，建立 code->档位名
    tmap = dl.get("type") or {}
    names = tmap.get("a") or []
    codes = tmap.get("b") or []
    code_to_name = {str(c): str(n) for c, n in zip(codes, names)}

    def col(key: str) -> list:
        v = L.get(key) or []
        return v if isinstance(v, list) else []

    hashes = col("m")
    titles = col("t")
    zhs = col("z")
    sizes = col("s")
    seeders = col("e")
    quals = col("p")
    times = col("n")
    count = max(len(hashes), len(titles))

    results: List[Dict[str, Any]] = []
    seen: set = set()
    for i in range(count):
        h = str(hashes[i]).strip() if i < len(hashes) else ""
        if not h:
            continue
        title = str(titles[i]).strip() if i < len(titles) else ""
        qcode = str(quals[i]).strip() if i < len(quals) else ""
        # 画质：优先站点代码，其次标题文本
        q_score, q_label = _quality_from_code(qcode)
        t_score, t_label = parse_quality(title)
        # 取较高分，标签随之；站点代码为主时保留其档位标签
        if t_score > q_score:
            q_score, q_label = t_score, t_label
        # 中字标记
        is_zh = bool(zhs[i]) if i < len(zhs) else False
        if is_zh and "中字" not in q_label:
            q_label = "中字" + q_label
            q_score += 4
        # 做种数（站点对未知值可能返回 -1，钳制到 0）
        try:
            seed = int(seeders[i]) if i < len(seeders) else 0
        except (ValueError, TypeError):
            seed = 0
        seed = max(0, seed)
        size = str(sizes[i]).strip() if i < len(sizes) else ""
        pub = str(times[i]).strip() if i < len(times) else ""
        # 生成标准磁力链接：btih + dn(标题 urlencode)
        dn = quote(title, safe="")
        url = f"magnet:?xt=urn:btih:{h}&dn={dn}"
        if url in seen:
            continue
        seen.add(url)
        results.append({
            "type": "magnet",
            "url": url,
            "title": title or "未命名资源",
            "size": size,
            "quality": q_score,
            "quality_label": q_label,
            "seeders": seed,
            "infohash": h.lower(),
            "pub_time": pub,
            "quality_code": qcode,
            "quality_name": code_to_name.get(qcode, ""),
        })
    return results


# ---------------------------------------------------------------- HTML 兜底解析器
def parse_115_link(url: str) -> Tuple[str, str]:
    url = url or ""
    share_code, receive_code = "", ""
    m = re.search(r"/s/([A-Za-z0-9_-]{4,})", url)
    if m:
        share_code = m.group(1)
    else:
        m = re.search(r"([A-Za-z0-9_-]{6,})-(\w{4})(?:[/\s]|$)", url)
        if m:
            return m.group(1), m.group(2)
    pm = re.search(r"[?&](?:password|pwd|receive_code|passcode)=([A-Za-z0-9]+)", url, re.I)
    if pm:
        receive_code = pm.group(1)
    return share_code, receive_code


def infohash_of(magnet: str) -> str:
    m = re.search(r"btih:([A-Za-z0-9]{32,})", magnet or "", re.I)
    return m.group(1).lower() if m else ""


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_resources(html: str, base_url: str = "") -> List[Dict[str, Any]]:
    """从 HTML 解析 115/磁力链接（兜底用，主路径请用 parse_downlist_json）。"""
    results: List[Dict[str, Any]] = []
    seen: set = set()
    soup = BeautifulSoup(html or "", "html.parser")

    def add(kind: str, raw_url: str, title: str, context: str) -> None:
        url = html_lib.unescape(raw_url).strip().rstrip(".,;:!?)]}>\"'`")
        if not url or url in seen:
            return
        seen.add(url)
        q_score, q_label = parse_quality(title + " " + context[:160])
        sm = re.search(r"(\d+(?:\.\d+)?\s*[TtGgMmKk][iI]?[Bb])", context)
        size = sm.group(1) if sm else ""
        item: Dict[str, Any] = {
            "type": kind, "url": url,
            "title": _clean(title)[:140] or "未命名资源",
            "size": size, "quality": q_score, "quality_label": q_label,
            "seeders": parse_seeders(context) if kind == "magnet" else 0,
        }
        if kind == "115":
            item["share_code"], item["receive_code"] = parse_115_link(url)
        else:
            item["infohash"] = infohash_of(url)
        results.append(item)

    for a in soup.find_all("a", href=True):
        href = a["href"]
        is_115 = bool(RE_115.search(href))
        is_magnet = bool(RE_MAGNET.search(href))
        if not (is_115 or is_magnet):
            continue
        title = a.get_text(" ", strip=True) or a.get("title", "")
        parent = a.parent
        context = parent.get_text(" ", strip=True) if parent else title
        if len(context) < len(title):
            context = title
        for m in (RE_115.finditer(href) if is_115 else RE_MAGNET.finditer(href)):
            add("115" if is_115 else "magnet", m.group(0), title, context)

    for m in RE_115.finditer(html or ""):
        add("115", m.group(0), "", html[max(0, m.start() - 260): m.end() + 200])
    for m in RE_MAGNET.finditer(html or ""):
        add("magnet", m.group(0), "", html[max(0, m.start() - 260): m.end() + 200])
    return results


# ---------------------------------------------------------------- 排序
def sort_resources(
    resources: List[Dict[str, Any]],
    quality_weight: float = 1.0,
    seeders_weight: float = 1.0,
) -> List[Dict[str, Any]]:
    """按『画质权重 + 做种数权重』加权打分排序，分数高者在前。"""
    def score(r: Dict[str, Any]) -> float:
        try:
            quality = float(r.get("quality", 0) or 0)
        except (TypeError, ValueError):
            quality = 0.0
        try:
            seeders = int(r.get("seeders", 0) or 0)
        except (TypeError, ValueError):
            seeders = 0
        # 站点对未知做种数可能返回 -1，钳制到 0 避免 math.log10(0) 报 domain error
        seeders = max(0, seeders)
        return quality * quality_weight + math.log10(seeders + 1) * 30.0 * seeders_weight
    return sorted(resources, key=score, reverse=True)


if __name__ == "__main__":
    demo = {
        "downlist": {
            "type": {"a": ["4K", "中字4K", "1080P", "中字1080P", "720P", "中字720P", "原盘", "未知"],
                     "b": ["i14", "i19", "i8", "i16", "i2", "i17", "i6", "i4"]},
            "list": {
                "m": ["a" * 40, "b" * 40],
                "t": ["Movie.2024.1080p.BluRay.x265", "Movie.2024.4K.HDR.REMUX"],
                "z": [0, 1], "s": ["7.37G", "54G"], "e": [9, 88],
                "p": ["i8", "i14"], "n": ["1月前", "2月前"],
            },
        }
    }
    rs = parse_downlist_json(demo)
    for r in sort_resources(rs):
        print(r["quality_label"], "做种", r["seeders"], r["title"][:40], r["url"][:60])
