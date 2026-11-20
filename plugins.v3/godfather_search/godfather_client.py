"""
教父站（www.教父.com / www.xn--wcv59z.com）搜索客户端。

站点真实结构（2026-11 实测）：
  - 搜索页 /search?q=<词> 只列影片，条目链接形如 /mv/mXr4；
  - 影片资源数据在详情页数据接口 GET /res/downurl/mv/mXr4（返回列式 JSON）；
  - 纯 requests 访问会被 PoW「浏览器安全验证」拦截，必须用真实浏览器完成 PoW；
  - 站内资源本质均为磁力（网盘分类走 keepshare 离线中转），无 115 分享直链。

流程：
  1. 浏览器带 Cookie 打开搜索页，过 PoW，提取影片详情链接；
  2. 进入详情页，用页面内 fetch 调 /res/downurl/... 拿 JSON；
  3. 交 resource_parser.parse_downlist_json() 解析、排序。

合规说明：
  - 本模块不破解鉴权、不打码、不撞库；登录态由用户本人提供的 Cookie 承担。
  - PoW 用真实浏览器环境自动计算，属于正常客户端行为。
"""
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from urllib.parse import quote, urljoin

from .resource_parser import parse_downlist_json, parse_resources, sort_resources


class GodfatherClient:
    DEFAULT_BASE = "https://www.xn--wcv59z.com"
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    def __init__(
        self,
        cookie: str = "",
        base_url: str = "",
        timeout: int = 25,
        use_browser: bool = True,
        no_proxy: bool = False,
        max_detail_pages: int = 3,
    ):
        self.cookie = (cookie or "").strip()
        self.base_url = (base_url or self.DEFAULT_BASE).rstrip("/")
        self.timeout = timeout
        self.use_browser = use_browser
        self.max_detail_pages = max_detail_pages
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.UA, "Accept-Language": "zh-CN,zh;q=0.9"})
        if self.cookie:
            self.session.headers.update({"Cookie": self.cookie})
        if no_proxy:
            self.session.trust_env = False
            self.session.proxies = {"http": None, "https": None}

    # -------------------------------------------------- Cookie
    def _cookie_dict(self) -> Dict[str, str]:
        result = {}
        for part in self.cookie.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                result[k.strip()] = v.strip()
        return result

    def _has_login(self) -> bool:
        keys = set(self._cookie_dict().keys())
        # 站点登录态关键是 app_auth；兼容其它常见字段
        return bool(keys & {"app_auth", "auth", "session", "uid", "user", "token", "PHPSESSID"})

    # -------------------------------------------------- 搜索入口
    def search(self, keyword: str, page: int = 1) -> Tuple[List[Dict], str]:
        """搜索影视资源，返回 (资源列表, 状态说明)。"""
        if not keyword:
            return [], "关键词为空"

        if not self.use_browser:
            # 纯 HTTP 模式：站点有 PoW，基本不可用；保留仅作兜底
            return [], "教父站启用了浏览器安全验证(PoW)，请开启「浏览器验证」选项"

        resources, err = self._search_by_browser(keyword)
        if err:
            return [], err
        if not resources:
            return [], "搜索完成但未解析到资源（关键词无结果、未登录或页面结构变化）"
        return resources, f"解析到 {len(resources)} 条资源"

    # -------------------------------------------------- 浏览器流程（同步 playwright）
    def _search_by_browser(self, keyword: str) -> Tuple[List[Dict], str]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return [], "缺少 playwright 依赖，请安装后重试（pip install playwright && playwright install chromium）"

        all_resources: List[Dict] = []
        seen_hash: set = set()
        detail_paths: List[str] = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
                )
                ctx = browser.new_context(
                    user_agent=self.UA, locale="zh-CN",
                    viewport={"width": 1280, "height": 900},
                )
                if self.cookie:
                    # 注入登录态 Cookie，但排除 browser_verified / PHPSESSID：
                    # 这两个由浏览器重新访问时自行生成，注入旧值会干扰 PoW 验证导致卡死。
                    ck = {
                        k: v for k, v in self._cookie_dict().items()
                        if k.lower() not in {"browser_verified", "phpsessid"}
                    }
                    for domain in (".xn--wcv59z.com", "www.xn--wcv59z.com"):
                        try:
                            ctx.add_cookies([
                                {"name": k, "value": v, "domain": domain, "path": "/"}
                                for k, v in ck.items()
                            ])
                        except Exception:
                            pass

                page = ctx.new_page()
                ms = self.timeout * 1000

                # 1) 打开搜索页（首页进一次过 PoW，再跳搜索页）
                try:
                    page.goto(self.base_url, wait_until="domcontentloaded", timeout=ms)
                    self._wait_pow(page)
                    search_url = f"{self.base_url}/search?q={quote(keyword)}"
                    page.goto(search_url, wait_until="domcontentloaded", timeout=ms)
                    self._wait_pow(page)
                    page.wait_for_timeout(5000)
                except Exception as e:
                    browser.close()
                    return [], f"打开搜索页失败: {str(e)[:120]}"

                html = page.content() or ""
                # 未登录检测
                if "立即登录" in html and "退出登录" not in html:
                    browser.close()
                    return [], "站点要求登录：请在插件配置中填写教父站 Cookie（浏览器登录后复制完整 Cookie）"

                # 2) 提取影片详情链接 /mv/xxx 或 /tv/xxx
                detail_paths = self._extract_detail_paths(html, page)
                if not detail_paths:
                    # 兜底：直接解析搜索页 HTML 里的磁力/115
                    fb = parse_resources(html, self.base_url)
                    browser.close()
                    return fb, "搜索页无详情链接，已尝试直接解析页面" if fb else "搜索页未找到影片结果"

                # 3) 逐个详情：进页面 -> fetch JSON 接口 -> 解析
                for path in detail_paths[: self.max_detail_pages]:
                    try:
                        page.goto(urljoin(self.base_url, path), wait_until="domcontentloaded", timeout=ms)
                        self._wait_pow(page)
                        page.wait_for_timeout(2000)
                        list_key = path.rstrip("/").split("/")[-1]  # 如 mXr4
                        api = f"/res/downurl/{path.strip('/')}"
                        raw = page.evaluate(
                            """async (u) => {
                                try {
                                    const r = await fetch(u, {credentials:'include'});
                                    return await r.text();
                                } catch(e) { return ''; }
                            }""",
                            api,
                        )
                        items = parse_downlist_json(raw)
                        if not items:
                            # 接口失败则解析详情页 HTML 兜底
                            items = parse_resources(page.content() or "", self.base_url)
                        for it in items:
                            h = it.get("infohash") or it.get("url")
                            if h and h not in seen_hash:
                                seen_hash.add(h)
                                all_resources.append(it)
                    except Exception:
                        continue

                # 保存刷新后的 Cookie（PoW 验证态）
                try:
                    self.cookie = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies())
                    self.session.headers.update({"Cookie": self.cookie})
                except Exception:
                    pass
                browser.close()
        except Exception as e:
            return [], f"浏览器搜索失败: {str(e)[:150]}"

        all_resources = sort_resources(all_resources)
        return all_resources, ""

    def _wait_pow(self, page, max_wait: int = 40) -> None:
        """等待 PoW 浏览器安全验证自动完成（站点计算较慢，需充分等待）。"""
        for _ in range(max_wait):
            try:
                content = page.content() or ""
                body = page.locator("body").inner_text() if page.locator("body").count() else ""
            except Exception:
                return
            # 站点 PoW 页面形态：浏览器安全验证 / 安全验证 / 验证完成后将自动继续 / 正在计算 x%
            # 只要出现验证相关字样（且没有影片内容），就继续等待
            powing = (
                "浏览器安全验证" in content
                or "powChallenge" in content
                or "安全验证" in body
                or ("验证" in body and "自动继续" in body)
            )
            if not powing:
                return
            page.wait_for_timeout(1000)

    def _extract_detail_paths(self, html: str, page) -> List[str]:
        """从搜索页提取影片详情链接，按标题相关性去重。"""
        paths: List[str] = []
        try:
            links = page.locator("a[href]").all()
        except Exception:
            links = []
        for a in links:
            try:
                href = (a.get_attribute("href") or "").strip()
            except Exception:
                continue
            # 形如 /mv/mXr4 或 /tv/xxxx
            if href.startswith("/mv/") or href.startswith("/tv/"):
                if href not in paths:
                    paths.append(href)
        return paths[: self.max_detail_pages]

    # -------------------------------------------------- 检查
    def check(self) -> Tuple[bool, str]:
        if not self._has_login():
            return False, "未检测到登录 Cookie（app_auth），请先在浏览器登录教父站并复制完整 Cookie"
        return True, "已配置 Cookie；登录态需实际搜索验证"

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass


if __name__ == "__main__":
    import sys
    kw = sys.argv[1] if len(sys.argv) > 1 else "测试"
    ck = sys.argv[2] if len(sys.argv) > 2 else ""
    c = GodfatherClient(cookie=ck)
    print("check:", c.check())
    rs, msg = c.search(kw)
    print(msg)
    for r in sort_resources(rs)[:5]:
        print(r["type"], r["quality_label"], "做种", r["seeders"], r["title"][:50])
