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
import re
import time
import threading
import contextlib
from typing import Any, Dict, List, Optional, Tuple

import requests
from urllib.parse import quote, urljoin, urlparse

# 两个插件实例（资源转存 + 订阅转存）在同一进程内可能同时触发浏览器搜索，
# 用进程内互斥锁 + 跨进程文件锁串行化浏览器登录/搜索，避免导航竞态与资源争抢。
_MP_LOCK = threading.RLock()
try:
    import fcntl
except (ImportError, ModuleNotFoundError):
    fcntl = None
_BROWSER_LOCK_FILE = "/tmp/godfather_browser.lock"


@contextlib.contextmanager
def _browser_guard():
    """浏览器操作串行锁：线程锁 + 文件锁，覆盖同进程多插件实例与跨进程调用。"""
    with _MP_LOCK:
        fh = None
        try:
            if fcntl:
                fh = open(_BROWSER_LOCK_FILE, "a+")
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if fh:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                fh.close()

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

    # -------------------------------------------------- 导航工具
    def _goto(self, page, url: str, wait_until: str = "domcontentloaded",
              timeout: int = None, attempts: int = 3) -> bool:
        """带「导航被另一导航中断」重试的 page.goto。

        站点首页 PoW 完成后会异步自动跳转回 /，若此时立即 goto 登录页，
        Playwright 抛 'Navigation ... is interrupted by another navigation'。
        这里捕获该异常，等待异步跳转结束后重试。返回是否成功。
        """
        last_err = None
        for i in range(attempts):
            try:
                page.goto(url, wait_until=wait_until,
                          timeout=(timeout or self.timeout) * 1000)
                return True
            except Exception as e:
                last_err = e
                msg = str(e).lower()
                if "interrupted by another navigation" in msg or "another navigation" in msg:
                    # 等待触发中断的那个异步跳转落定后再试
                    try:
                        page.wait_for_timeout(1200 + i * 800)
                    except Exception:
                        pass
                    continue
                raise
        raise last_err

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

        # 串行化浏览器搜索：避免两个插件同时开浏览器导致 PoW 导航竞态/资源争抢
        with _browser_guard():
            resources, err = self._search_by_browser(keyword)
        if err:
            return [], err
        if not resources:
            return [], "搜索完成但未解析到资源（关键词无结果、未登录或页面结构变化）"
        return resources, f"解析到 {len(resources)} 条资源"

    # -------------------------------------------------- 两级搜索（影视列表 → 精确资源）
    def _open_logged_page(self, p):
        """启动浏览器 → 过首页 PoW → 登录，返回 (browser, ctx, page, err)。
        调用方负责在 finally 里 close(browser)。供两级搜索复用，不影响原 search()。"""
        browser, launch_err = self._launch_browser(p)
        if not browser:
            return None, None, None, launch_err
        ctx = browser.new_context(user_agent=self.UA, locale="zh-CN",
                                  viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        try:
            self._goto(page, self.base_url, wait_until="domcontentloaded",
                       timeout=self.timeout)
            self._wait_pow(page)
            if self.username and self.password:
                ok, msg = self._browser_login(page, ctx)
                if not ok:
                    browser.close()
                    return None, None, None, msg
            elif self.cookie:
                ck = {k: v for k, v in self._cookie_dict().items()
                      if k.lower() not in {"browser_verified", "phpsessid"}}
                for domain in (".xn--wcv59z.com", "www.xn--wcv59z.com"):
                    try:
                        ctx.add_cookies([
                            {"name": k, "value": v, "domain": domain, "path": "/"}
                            for k, v in ck.items()
                        ])
                    except Exception:
                        pass
        except Exception as e:
            browser.close()
            return None, None, None, f"初始化浏览器失败: {str(e)[:120]}"
        return browser, ctx, page, ""

    def _save_context_cookie(self, ctx):
        """保存刷新后的登录态 Cookie，便于后续复用。"""
        try:
            self.cookie = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies())
            self.session.headers.update({"Cookie": self.cookie})
        except Exception:
            pass

    def search_media_list(self, keyword: str) -> Tuple[List[Dict], str]:
        """第一级：模糊搜索，返回影视卡片列表（每个卡片=一部影视，含详情路径与标题）。"""
        if not keyword:
            return [], "关键词为空"
        if not self.use_browser:
            return [], "请开启「浏览器验证」选项"
        with _browser_guard():
            return self._search_media_list_by_browser(keyword)

    def _search_media_list_by_browser(self, keyword: str) -> Tuple[List[Dict], str]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return [], "缺少 playwright 依赖，请安装后重试"
        cards: List[Dict] = []
        try:
            with sync_playwright() as p:
                browser, ctx, page, err = self._open_logged_page(p)
                if not browser:
                    return [], err
                try:
                    search_url = f"{self.base_url}/search?q={quote(keyword)}"
                    self._goto(page, search_url, wait_until="domcontentloaded",
                               timeout=self.timeout)
                    self._wait_pow(page)
                    # 充分等待 + 滚动触发懒加载，避免只抓到首屏少数卡片
                    page.wait_for_timeout(4000)
                    self._scroll_load_all(page)
                    cards = self._extract_media_cards(page)
                    self._save_context_cookie(ctx)
                finally:
                    browser.close()
        except Exception as e:
            return [], f"搜索影视列表失败: {str(e)[:150]}"
        if not cards:
            return [], "搜索页未找到影视卡片（关键词无结果或页面结构变化）"
        return cards, ""

    def _scroll_load_all(self, page) -> None:
        """滚动页面到底部触发懒加载/无限滚动，尽量让全部影视卡片渲染出来。"""
        try:
            last = -1
            for _ in range(8):
                cur = page.evaluate("document.body.scrollHeight")
                if cur == last:
                    break
                last = cur
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(800)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(300)
        except Exception:
            pass

    @staticmethod
    def _norm_detail_path(href: str, base_url: str) -> str:
        """把各种形态的详情链接归一化为 /mv/xxx 或 /tv/xxx。
        兼容：/mv/xxx、完整 https URL、//域名/mv/xxx、带 ?query/#anchor、末尾斜杠。"""
        if not href:
            return ""
        try:
            full = urljoin(base_url, href)
            parsed = urlparse(full)
            path = parsed.path or ""
        except Exception:
            path = href.split("?")[0].split("#")[0]
        m = re.search(r"/(mv|tv)/([A-Za-z0-9_-]+)", path)
        if not m:
            return ""
        return f"/{m.group(1)}/{m.group(2)}"

    def _extract_media_cards(self, page) -> List[Dict]:
        """从搜索页提取影视卡片：按详情路径去重，标题取同一路径下信息量最大的文本。
        旧实现只认 '/mv/' 开头的相对 href，完整 URL / 带 query / 相对 './mv/x' 会被漏掉。"""
        best: Dict[str, str] = {}
        order: List[str] = []
        try:
            links = page.locator("a[href]").all()
        except Exception:
            links = []
        for a in links:
            try:
                href = (a.get_attribute("href") or "").strip()
            except Exception:
                continue
            canon = self._norm_detail_path(href, self.base_url)
            if not canon:
                continue
            # 标题来源优先级：链接文本 → title 属性 → img[alt] → 父容器文本
            title = ""
            try:
                title = (a.inner_text() or "").strip()
            except Exception:
                pass
            if len(title) < 6:
                try:
                    t = (a.get_attribute("title") or "").strip()
                    if len(t) > len(title):
                        title = t
                except Exception:
                    pass
            if len(title) < 6:
                try:
                    for img in a.locator("img").all()[:1]:
                        alt = (img.get_attribute("alt") or "").strip()
                        if len(alt) > len(title):
                            title = alt
                except Exception:
                    pass
            if len(title) < 6:
                try:
                    parent_text = (a.locator("xpath=..").inner_text() or "").strip()
                    if len(parent_text) > len(title) and len(parent_text) <= 120:
                        title = parent_text
                except Exception:
                    pass
            title = re.sub(r"\s+", " ", title).strip()[:100]
            if canon not in best:
                best[canon] = ""
                order.append(canon)
            if len(title) > len(best[canon]):
                best[canon] = title
        return [
            {"path": href, "title": best[href] or href.rsplit("/", 1)[-1]}
            for href in order
        ]

    def search_media_resources(self, media_path: str) -> Tuple[List[Dict], str]:
        """第二级：按影视详情路径精确取该影视的全部资源。"""
        if not media_path:
            return [], "影视路径为空"
        if not self.use_browser:
            return [], "请开启「浏览器验证」选项"
        with _browser_guard():
            return self._resources_by_path(media_path)

    def _resources_by_path(self, path: str) -> Tuple[List[Dict], str]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return [], "缺少 playwright 依赖，请安装后重试"
        found: List[Dict] = []
        seen: set = set()
        try:
            with sync_playwright() as p:
                browser, ctx, page, err = self._open_logged_page(p)
                if not browser:
                    return [], err
                try:
                    self._goto(page, urljoin(self.base_url, path),
                               wait_until="domcontentloaded", timeout=self.timeout)
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
                        if h and h not in seen:
                            seen.add(h)
                            found.append(it)
                    self._save_context_cookie(ctx)
                finally:
                    browser.close()
        except Exception as e:
            return [], f"读取影视资源失败: {str(e)[:150]}"
        if not found:
            return [], "该影视未解析到资源（详情接口结构可能变化）"
        return sort_resources(found), ""

    # -------------------------------------------------- 浏览器登录
    def _browser_login(self, page, ctx) -> Tuple[bool, str]:
        """在浏览器里用账号密码登录；登录态保留在同一 context。返回 (是否成功, 说明)。"""
        ms = self.timeout * 1000
        try:
            self._goto(page, urljoin(self.base_url, self.LOGIN_PATH),
                       wait_until="domcontentloaded", timeout=self.timeout)
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
        """多策略启动 Chromium；浏览器缺失时尝试在容器内自动安装（自愈）。

        新版 Playwright 的 headless=True 默认找 chrome-headless-shell，而 MoviePilot
        镜像常未下载任何浏览器二进制。策略顺序：
          1) channel="chromium"（完整 Chromium，自带新版无头模式）
          2) 常见系统 Chrome/Chromium 可执行文件路径
          3) 默认 headless（已装 headless-shell 的环境）
          4) 若都失败且是「浏览器未安装」，自动 playwright install 后重试一次
        返回 (browser, None) 或 (None, 错误信息)。
        """
        base_args = ["--no-sandbox", "--disable-blink-features=AutomationControlled",
                     "--disable-dev-shm-usage"]

        def try_all():
            errs = []
            try:
                return p.chromium.launch(headless=True, channel="chromium", args=base_args), None
            except Exception as e:
                errs.append(f"channel=chromium: {str(e)[:110]}")
            for path in (
                "/usr/bin/chromium", "/usr/bin/chromium-browser",
                "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
                "/usr/bin/chrome", "/opt/google/chrome/chrome",
            ):
                if os.path.exists(path):
                    try:
                        return p.chromium.launch(headless=True, executable_path=path, args=base_args), None
                    except Exception as e:
                        errs.append(f"{path}: {str(e)[:110]}")
            try:
                return p.chromium.launch(headless=True, args=base_args), None
            except Exception as e:
                errs.append(f"默认: {str(e)[:110]}")
            return None, errs

        browser, errors = try_all()
        if browser:
            return browser, None

        joined = " | ".join(errors)
        # 仅在确实是「浏览器二进制缺失」时自动安装，避免掩盖其它启动错误
        if "Executable doesn't exist" in joined or "executable doesn't exist" in joined:
            ok, install_msg = self._auto_install_browser(p, try_all)
            if ok:
                return install_msg, None  # install_msg 此时是 browser
            joined += " || " + install_msg

        hint = (
            "浏览器未安装，且自动安装未成功。请在 MoviePilot 容器内执行后重启：\n"
            "  python -m playwright install chromium-headless-shell\n"
            "  或完整版：python -m playwright install chromium\n"
            "  （若报 playwright 模块不存在，先 pip install playwright）\n"
            "  详情: " + joined[:400]
        )
        return None, hint

    def _auto_install_browser(self, p, retry_fn):
        """在容器内自动下载 Playwright 浏览器二进制。

        用运行插件的同一个 Python 解释器执行 playwright install，确保浏览器装到
        当前 playwright 使用的缓存目录。返回 (browser, None) 成功，或 (None, 错误说明)。
        """
        import subprocess
        import sys
        attempts = [
            [sys.executable, "-m", "playwright", "install", "chromium-headless-shell"],
            [sys.executable, "-m", "playwright", "install", "chromium"],
            ["playwright", "install", "chromium-headless-shell"],
        ]
        logs = []
        for cmd in attempts:
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=420)
                logs.append(f"{cmd[-1]} rc={proc.returncode}")
                if proc.returncode == 0:
                    browser, _ = retry_fn()
                    if browser:
                        return browser, None
            except subprocess.TimeoutExpired:
                logs.append(f"{cmd[-1]} 超时")
            except FileNotFoundError:
                logs.append(f"{cmd[-1]} 命令不存在")
            except Exception as e:
                logs.append(f"{cmd[-1]} {str(e)[:80]}")
        return None, "自动安装尝试: " + " | ".join(logs)

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
                    self._goto(page, self.base_url, wait_until="domcontentloaded",
                               timeout=self.timeout)
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
                    self._goto(page, search_url, wait_until="domcontentloaded",
                               timeout=self.timeout)
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
                        self._goto(page, urljoin(self.base_url, path),
                                   wait_until="domcontentloaded", timeout=self.timeout)
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
