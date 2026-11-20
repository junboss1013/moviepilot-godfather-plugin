"""
教父站（www.教父.com / www.xn--wcv59z.com）搜索客户端。

职责：
  1. 使用用户提供的站点 Cookie 访问搜索页 /search?q=<关键词>；
  2. 处理站点的 PoW 浏览器验证（通过 Playwright 有头/无头自动完成）；
  3. 返回搜索结果 HTML，交给 resource_parser 解析。

合规说明：
  - 本模块不破解、不绕过站点的登录鉴权；登录态由用户自己提供的 Cookie 承担。
  - PoW 是站点的反爬校验，用真实浏览器环境自动完成计算，属于正常的客户端行为。
  - 若站点要求验证码/登录，插件会提示用户补充 Cookie，不会自动打码或撞库。
"""
import time
from typing import Dict, List, Optional, Tuple

import requests

from .resource_parser import parse_resources, sort_resources


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
    ):
        self.cookie = (cookie or "").strip()
        self.base_url = (base_url or self.DEFAULT_BASE).rstrip("/")
        self.timeout = timeout
        self.use_browser = use_browser
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.UA, "Accept-Language": "zh-CN,zh;q=0.9"})
        if self.cookie:
            self.session.headers.update({"Cookie": self.cookie})
        if no_proxy:
            self.session.trust_env = False
            self.session.proxies = {"http": None, "https": None}
        self._playwright_ready: Optional[bool] = None

    # -------------------------------------------------- Cookie 解析
    def _cookie_dict(self) -> Dict[str, str]:
        result = {}
        for part in self.cookie.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                result[k.strip()] = v.strip()
        return result

    def _has_login(self) -> bool:
        """粗判是否含登录态 Cookie。"""
        keys = set(self._cookie_dict().keys())
        return bool(keys & {"session", "uid", "user", "token", "auth", "sess", "PHPSESSID", "user_session"})

    # -------------------------------------------------- 搜索入口
    def search(self, keyword: str, page: int = 1) -> Tuple[List[Dict], str]:
        """
        搜索影视资源。
        :return: (资源列表, 状态说明)
        """
        if not keyword:
            return [], "关键词为空"

        url = f"{self.base_url}/search"
        params = {"q": keyword}
        if page > 1:
            params["page"] = page

        html, err = self._fetch(url, params)
        if err:
            return [], err

        # 未登录检测
        if "登录后访问受限内容" in html or ("立即登录" in html and "搜索" not in html):
            return [], "站点要求登录：请在插件配置中填写教父站 Cookie（浏览器登录后复制）"

        resources = parse_resources(html, self.base_url)
        if not resources:
            return [], "搜索完成但未解析到 115/磁力资源（可能关键词无结果，或页面结构变化）"

        return resources, f"解析到 {len(resources)} 条资源"

    # -------------------------------------------------- 抓取（含 PoW）
    def _fetch(self, url: str, params: Dict) -> Tuple[str, str]:
        """抓取页面，优先用 HTTP，检测到 PoW 时切换浏览器。"""
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            html = resp.text
            if "浏览器安全验证" not in html and "powChallenge" not in html:
                return html, ""
        except Exception:
            pass

        if not self.use_browser:
            return "", "命中浏览器安全验证(PoW)，请开启「浏览器验证」选项或更新 Cookie"

        return self._fetch_by_browser(url, params)

    def _fetch_by_browser(self, url: str, params: Dict) -> Tuple[str, str]:
        """用 Playwright + Chromium 自动完成 PoW 后抓取页面。"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return "", "缺少 playwright 依赖，请在 requirements.txt 安装后重试"

        query = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())
        target = f"{url}?{query}" if query else url
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
                )
                ctx = browser.new_context(
                    user_agent=self.UA,
                    locale="zh-CN",
                    viewport={"width": 1280, "height": 800},
                )
                if self.cookie:
                    ctx.add_cookies([
                        {"name": k, "value": v, "domain": ".xn--wcv59z.com", "path": "/"}
                        for k, v in self._cookie_dict().items()
                    ])

                page = ctx.new_page()
                page.goto(self.base_url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                # 等待 PoW 自动完成（最多 15 秒）
                for _ in range(15):
                    if "浏览器安全验证" not in (page.content() or ""):
                        break
                    page.wait_for_timeout(1000)

                page.goto(target, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                page.wait_for_timeout(2000)
                html = page.content()

                # 保存最新 Cookie 供后续复用
                self.cookie = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies())
                self.session.headers.update({"Cookie": self.cookie})
                browser.close()
                return html, ""
        except Exception as e:
            return "", f"浏览器抓取失败: {e}"

    def check(self) -> Tuple[bool, str]:
        """检查站点可达性与登录态。"""
        html, err = self._fetch(self.base_url, {})
        if err:
            return False, err
        if "登录后访问受限内容" in html:
            return self._has_login(), (
                "站点可访问，但未检测到登录 Cookie" if not self._has_login()
                else "站点可访问且已配置 Cookie（登录态需实际搜索验证）"
            )
        return True, "站点可访问"

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
        print(r["type"], r["quality_label"], r["seeders"], r["title"][:50])
