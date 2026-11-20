"""
教父站搜索结果解析器。

职责：
  1. 从搜索结果 HTML 中提取 115 分享链接与磁力链接；
  2. 识别画质（4K/1080p/720p/REMUX/HDR...）与做种数；
  3. 按「画质权重 + 做种数权重」对磁力资源打分排序。

本模块不依赖 MoviePilot，可独立单元测试。
"""
import html as html_lib
import math
import re
from typing import Any, Dict, List, Tuple

from bs4 import BeautifulSoup


# ---------------------------------------------------------------- 链接正则
# 兼容 115.com / 115cdn.com / share.115.com / 115vod.com
RE_115 = re.compile(
    r"https?://(?:[\w-]+\.)*115(?:cdn|vod)?\.com/s/[A-Za-z0-9_-]{4,}[^\s<>\"'`\]]*",
    re.I,
)
# magnet:?xt=urn:btih:<40位hex 或 32位base32>
RE_MAGNET = re.compile(r"magnet:\?xt=urn:btih:[A-Za-z0-9]{32,}[^\s<>\"'`\]]*", re.I)
RE_PASSWORD = re.search  # 占位，避免误用；实际使用 re.search


# ---------------------------------------------------------------- 画质识别
def parse_quality(text: str) -> Tuple[int, str]:
    """返回 (画质分数, 画质标签)。分数越高画质越好。"""
    t = (text or "").lower()
    score, label = 20, "未知"

    if re.search(r"(4k|uhd|2160p|2160i)", t):
        score, label = 100, "4K"
    elif re.search(r"(1080p|fhd|full[\s-]?hd|bluray|blu[\s-]?ray|蓝光|原盘|bdrm?vb?|bdrip|bdremux)", t):
        score, label = 80, "1080p"
    elif re.search(r"(720p|hd[\s.-]?tv|hdtv)", t):
        score, label = 60, "720p"
    elif re.search(r"(480p|576p|\bsd\b|dvd|vcd)", t):
        score, label = 30, "标清"

    bonus = 0
    if re.search(r"remux", t):
        bonus += 15
    if re.search(r"(x265|h[\s.]?265|hevc|10bit|10[\s-]bit)", t):
        bonus += 6
    if re.search(r"(hdr|dolby[\s-]?vision|\bdv\b|hdr10)", t):
        bonus += 4
    if re.search(r"(atmos|truehd|dts|dd5\.1|5\.1ch)", t):
        bonus += 3
    return score + bonus, label


# ---------------------------------------------------------------- 做种数识别
_SEED_PATTERNS = [
    r"做种\s*[:：]?\s*(\d+)",
    r"(\d+)\s*个?做种",
    r"种子(?:数)?\s*[:：]?\s*(\d+)",
    r"seeders?\s*[:：]?\s*(\d+)",
    r"(\d+)\s*seeders?",
    r"热度\s*[:：]?\s*(\d+)",
    r"♥\s*(\d+)",
    r"\bS\s*[:：]\s*(\d+)",
]


def parse_seeders(text: str) -> int:
    """从文本中提取做种数，取不到返回 0。"""
    t = text or ""
    for pattern in _SEED_PATTERNS:
        m = re.search(pattern, t, re.I)
        if m:
            try:
                return int(m.group(1))
            except (ValueError, IndexError):
                continue
    return 0


# ---------------------------------------------------------------- 链接解析
def parse_115_link(url: str) -> Tuple[str, str]:
    """解析 115 分享链接，返回 (share_code, receive_code)。"""
    url = url or ""
    share_code, receive_code = "", ""

    m = re.search(r"/s/([A-Za-z0-9_-]{4,})", url)
    if m:
        share_code = m.group(1)
    else:
        # 兼容 {share_code}-{receive_code} / #{share_code}-{receive_code}# 形式
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
    text = re.sub(r"\s+", " ", text or "")
    return text.strip()


def parse_resources(html: str, base_url: str = "") -> List[Dict[str, Any]]:
    """
    从搜索结果 HTML 解析资源列表。

    返回条目结构：
      {
        "type": "115" | "magnet",
        "url": 原始链接,
        "title": 标题,
        "size": 大小文本,
        "quality": 画质分数,
        "quality_label": 画质标签,
        "seeders": 做种数,
        "share_code"/"receive_code" 或 "infohash"
      }
    """
    results: List[Dict[str, Any]] = []
    seen: set = set()
    soup = BeautifulSoup(html or "", "html.parser")

    def add(kind: str, raw_url: str, title: str, context: str) -> None:
        # 规范化：HTML 实体反转义（&amp; -> &），避免同一链接因编码差异被重复解析
        url = html_lib.unescape(raw_url).strip().rstrip(".,;:!?)]}>\"'`")
        if not url or url in seen:
            return
        seen.add(url)

        q_score, q_label = parse_quality(title + " " + context[:160])
        size = ""
        sm = re.search(r"(\d+(?:\.\d+)?\s*[TtGgMmKk][iI]?[Bb])", context)
        if sm:
            size = sm.group(1)

        item: Dict[str, Any] = {
            "type": kind,
            "url": url,
            "title": _clean(title)[:140] or "未命名资源",
            "size": size,
            "quality": q_score,
            "quality_label": q_label,
            "seeders": parse_seeders(context) if kind == "magnet" else 0,
        }
        if kind == "115":
            share_code, receive_code = parse_115_link(url)
            item["share_code"] = share_code
            item["receive_code"] = receive_code
        else:
            item["infohash"] = infohash_of(url)
        results.append(item)

    # 优先：从 <a> 标签提取（能拿到更准确的标题与上下文）
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
        for m in RE_115.finditer(href) if is_115 else RE_MAGNET.finditer(href):
            add("115" if is_115 else "magnet", m.group(0), title, context)

    # 兜底：正文中裸露的链接（有些站点用 JS 渲染或纯文本展示）
    for m in RE_115.finditer(html or ""):
        add("115", m.group(0), "", html[max(0, m.start() - 260): m.end() + 200])
    for m in RE_MAGNET.finditer(html or ""):
        add("magnet", m.group(0), "", html[max(0, m.start() - 260): m.end() + 200])

    return results


def sort_resources(
    resources: List[Dict[str, Any]],
    quality_weight: float = 1.0,
    seeders_weight: float = 1.0,
) -> List[Dict[str, Any]]:
    """
    按「画质权重 + 做种数权重」加权打分排序（分数高者在前）。
    做种数采用 log10 归一化，避免超大做种数完全压制画质差异。
    """

    def score(r: Dict[str, Any]) -> float:
        quality = float(r.get("quality", 0))
        seeders = int(r.get("seeders", 0) or 0)
        seed_score = math.log10(seeders + 1) * 30.0
        return quality * quality_weight + seed_score * seeders_weight

    return sorted(resources, key=score, reverse=True)


if __name__ == "__main__":
    demo = """
    <div class="item">
      <a href="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Test.2024.1080p.BluRay.x265">
        Test.2024.1080p.BluRay.x265 做种 42  18.6GB
      </a>
    </div>
    <div class="item">
      <a href="https://115.com/s/abc123def?password=wxyz">Test.2024.4K.REMUX</a>
    </div>
    """
    rs = parse_resources(demo)
    for r in rs:
        print(r["type"], r["quality_label"], r["seeders"], r["title"][:40])
    print("--- sorted magnets ---")
    for r in sort_resources([x for x in rs if x["type"] == "magnet"]):
        print(r["quality_label"], r["seeders"], r["title"][:40])
