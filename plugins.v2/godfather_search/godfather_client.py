"""
教父站（www.教父.com / www.xn--wcv59z.com）搜索客户端。

站点真实结构（2026-11 实测）：
  - 搜索页 /search?q=<词> 只列影片，条目链接形如 /mv/mXr4；
  - 影片资源数据在详情页数据接口 GET /res/downurl/mv/mXr4（返回列式 JSON）；
  - 纯 requests 访问会被 PoW「浏览器安全验证」拦截，必须用真实浏览器完成 PoW；
  - 站内资源本质均为磁力（网盘分类走 keepshare 离线中转），无 115 分享直链。

登录方式（二选一，优先账号密码）：
  1. 账号 + 密码：浏览器打开 /user/login，自动填写 username/password 并登录；
     实测表单字段：input[name=username]、input[name=password]、button[type=submit]。
  2. Cookie 直填（兼容旧配置）：浏览器登录后复制完整 Cookie。
  两种方式的登录态都保留在同一个浏览器上下文里，再走 PoW + 搜索流程。

流程：
  1. 浏览器完成登录（账号密码或 Cookie）+ 过 PoW，打开搜索页提取详情链接；
  2. 进入详情页，用页面内 fetch 调 /res/downurl/... 拿 JSON；
  3. 交 resource_parser.parse_downlist_json() 解析、排序。

合规说明：
  - 本模块只用用户本人提供的账号密码/Cookie 登录其自有账号，不破解鉴权、不打码、不撞库。
  - PoW 用真实浏览器环境自动计算，属于正常客户端行为。
"""
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from urllib.parse import quote, urljoin

from .resource_parser import parse_downlist_json, parse_resources, sort_resources


class GodfatherClient:
    DEFAULT_BASE = "https://www.xn--wcv59z.com"
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    LOGIN_PATH = "/user/login"

    def __init__(
        self,
        cookie: str = "",
        username: str = "",
        password: str = "",
        base_url: str = "",
        timeout: int = 25,
        use_browser: bool = True,
        no_proxy: bool = False,
        max_detail_pages: int = 3,
    ):
        self.cookie = (cookie or "").strip()
        self.username = (username or "").strip()
        self.password = (password or "").strip()
        self.base_url = (base_url or self.DEFAULT_BASE).rstrip("/")
        self.timeout = timeout
        self.use_browser = use_browser
        self.max_detail_pages = max_detail_pages
        # 登录态是否已在某次浏览器会话中确认（用于 check 后复用判断）
        self._logged_in = False
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
        """是否具备任意一种登录凭据。"""
        if self.username and self.password:
            return True
        keys = set(self._cookie_dict().keys())
        return bool(keys & {"app_auth", "auth", "session", "uid", "user", "token", "PHPSESSID"})

    # -------------------------------------------------- 搜索入口
    def search(self, keyword: str, page: int = 1) -> Tuple[List[Dict], str]:
        """搜索影视资源，返回 (资源列表, 状态说明)。"""
        if not keyword:
            return [], "关键词为空"
        if not self.use_browser:
            return [], "教父站启用了浏览器安全验证(PoW)，请开启「浏览器验证」选项"

        resources, err = self._search_by_browser(keyword)
        if err:
            return [], err
        if not resources:
            return [], "搜索完成但未解析到资源（关键词无结果、未登录或页面结构变化）"
        return resources, f"解析到 {len(resources)} 条资源"

    # -------------------------------------------------- 浏览器登录
    def _browser_login(self, page, ctx) -> Tuple[bool, str]:
        """在浏览器里用账号密码登录；登录态保留在同一 context。返回 (是否成功, 说明)。"""
        ms = self.timeout * 1000
        try:
            page.goto(urljoin(self.base_url, self.LOGIN_PATH),
                      wait_until="domcontentloaded", timeout=ms)
            self._wait_pow(page, max_wait=30)
            page.wait_for_timeout(1200)

            user_sel = 'input[name="username"]'
            pwd_sel = 'input[name="password"]'
            if page.locator(user_sel).count() == 0 or page.locator(pwd_sel).count() == 0:
                return False, "登录页未找到账号/密码输入框，站点结构可能已变化"

            page.locator(user_sel).first.fill(self.username)
            page.locator(pwd_sel).first.fill(self.password)
            # 勾选「记住登录」，便于维持会话
            try:
                cb = page.locator('input[name="cookietime"]')
                if cb.count() and not cb.first.is_checked():
                    cb.first.check()
            except Exception:
                pass

            submit = page.locator('button[type="submit"]')
            if submit.count():
                submit.first.click()
            else:
                page.locator(pwd_sel).first.press("Enter")
            page.wait_for_timeout(2500)
            self._wait_pow(page, max_wait=30)
            page.wait_for_timeout(1500)

            try:
                body = page.locator("body").inner_text() if page.locator("body").count() else ""
            except Exception:
                body = ""

            # 登录失败特征
            for bad in ("账号或密码错误", "用户名或密码错误", "密码错误", "账号不存在", "用户名不存在"):
                if bad in body:
                    return False, f"登录失败：{bad}，请检查账号密码"

            # 登录成功特征：出现 app_auth Cookie 或「退出登录」
            cookie_names = {c["name"] for c in ctx.cookies()}
            if "app_auth" in cookie_names or "退出登录" in body or "退出" in body:
                self._logged_in = True
                return True, ""

            return False, "登录未完成（未检测到登录态），可能是安全验证未通过，请稍后重试"
        except Exception as e:
            return False, f"浏览器自动登录失败: {str(e)[:150]}"

    def _launch_browser(self, p):
        """多策略启动 Chromium。

        新版 Playwright 的 headless=True 默认找 chrome-headless-shell，而 MoviePilot
        镜像常只装了完整 chromium 或系统 Chrome。按以下顺序回退，任一成功即用：
          1) channel="chromium"（完整 Chromium，自带新版无头模式）
          2) 常见系统 Chrome/Chromium 可执行文件路径
          3) 默认 headless（本地/已装 headless-shell 的环境）
        返回 (browser, None) 或 (None, 错误信息)。
        """
        base_args = ["--no-sandbox", "--disable-blink-features=AutomationControlled",
                     "--disable-dev-shm-usage"]
        errors = []

        # 1) 完整 Chromium channel（绕开 chrome-headless-shell 依赖）
        try:
            browser = p.chromium.launch(headless=True, channel="chromium", args=base_args)
            return browser, None
        except Exception as e:
            errors.append(f"channel=chromium: {str(e)[:120]}")

        # 2) 系统已安装的 Chrome/Chromium 可执行文件
        for path in (
            "/usr/bin/chromium", "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
            "/usr/bin/chrome", "/opt/google/chrome/chrome",
        ):
            if os.path.exists(path):
                try:
                    browser = p.chromium.launch(headless=True, executable_path=path, args=base_args)
                    return browser, None
                except Exception as e:
                    errors.append(f"{path}: {str(e)[:120]}")

        # 3) 默认（本地开发或已装 headless-shell 的环境）
        try:
            browser = p.chromium.launch(headless=True, args=base_args)
            return browser, None
        except Exception as e:
            errors.append(f"默认: {str(e)[:120]}")

        hint = (
            "浏览器未安装。请在 MoviePilot 容器内执行安装命令后重启：\n"
            "  pip install playwright && playwright install chromium\n"
            "  （若空间有限可只装无头版：playwright install chromium-headless-shell）\n"
            "  详情: " + " | ".join(errors[:3])
        )
        return None, hint

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
                browser, launch_err = self._launch_browser(p)
                if not browser:
                    return [], launch_err
                ctx = browser.new_context(
                    user_agent=self.UA, locale="zh-CN",
                    viewport={"width": 1280, "height": 900},
                )
                page = ctx.new_page()
                ms = self.timeout * 1000

                # 先过首页 PoW，建立基础 Cookie / 完成安全验证
                try:
                    page.goto(self.base_url, wait_until="domcontentloaded", timeout=ms)
                    self._wait_pow(page)
                except Exception as e:
                    browser.close()
                    return [], f"打开站点首页失败: {str(e)[:120]}"

                # 登录：优先账号密码，兼容 Cookie
                if self.username and self.password:
                    ok, msg = self._browser_login(page, ctx)
                    if not ok:
                        browser.close()
                        return [], msg
                elif self.cookie:
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

                # 1) 打开搜索页
                try:
                    search_url = f"{self.base_url}/search?q={quote(keyword)}"
                    page.goto(search_url, wait_until="domcontentloaded", timeout=ms)
                    self._wait_pow(page)
                    page.wait_for_timeout(5000)
                except Exception as e:
                    browser.close()
                    return [], f"打开搜索页失败: {str(e)[:120]}"

                html = page.content() or ""
                # 未登录检测（仅当完全没凭据时给出配置提示）
                if "立即登录" in html and "退出登录" not in html:
                    browser.close()
                    if self.username and self.password:
                        return [], "登录态失效：账号密码未能维持登录，请检查账号密码或稍后重试"
                    return [], "站点要求登录：请在插件配置中填写教父站账号密码（或 Cookie）"

                # 2) 提取影片详情链接 /mv/xxx 或 /tv/xxx
                detail_paths = self._extract_detail_paths(html, page)
                if not detail_paths:
                    fb = parse_resources(html, self.base_url)
                    browser.close()
                    return fb, "搜索页无详情链接，已尝试直接解析页面" if fb else "搜索页未找到影片结果"

                # 3) 逐个详情：进页面 -> fetch JSON 接口 -> 解析
                for path in detail_paths[: self.max_detail_pages]:
                    try:
                        page.goto(urljoin(self.base_url, path), wait_until="domcontentloaded", timeout=ms)
                        self._wait_pow(page)
                        page.wait_for_timeout(2000)
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
                            items = parse_resources(page.content() or "", self.base_url)
                        for it in items:
                            h = it.get("infohash") or it.get("url")
                            if h and h not in seen_hash:
                                seen_hash.add(h)
                                all_resources.append(it)
                    except Exception:
                        continue

                # 保存刷新后的 Cookie（PoW 验证态），便于后续复用
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
            if href.startswith("/mv/") or href.startswith("/tv/"):
                if href not in paths:
                    paths.append(href)
        return paths[: self.max_detail_pages]

    # -------------------------------------------------- 检查
    def check(self) -> Tuple[bool, str]:
        if self.username and self.password:
            return True, "已配置账号密码；登录将在搜索时通过浏览器自动完成"
        if self._has_login():
            return True, "已配置 Cookie；登录态需实际搜索验证"
        return False, "请配置教父站账号密码（推荐）或 Cookie"

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass


if __name__ == "__main__":
    import sys
    kw = sys.argv[1] if len(sys.argv) > 1 else "测试"
    user = sys.argv[2] if len(sys.argv) > 2 else ""
    pwd = sys.argv[3] if len(sys.argv) > 3 else ""
    ck = sys.argv[4] if len(sys.argv) > 4 else ""
    c = GodfatherClient(username=user, password=pwd, cookie=ck)
    print("check:", c.check())
    rs, msg = c.search(kw)
    print(msg)
    for r in sort_resources(rs)[:5]:
        print(r["type"], r["quality_label"], "做种", r["seeders"], r["title"][:50])
