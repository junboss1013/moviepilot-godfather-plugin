"""
转存成功后触发 strm-gen webhook，让 Emby 媒体库立即刷新。

原理：strm-gen 容器常驻一个 HTTP 服务（POST /refresh），收到请求立即重新扫描
115 网盘目录、生成 strm、写入 Emby 并触发媒体库刷新。插件转存成功后 POST 一下，
不再需要等到 cron 时间点。

调用方只需把本模块的 trigger_refresh(plugin) 接在转存成功处；插件实例上需有：
  - self._webhook_url   完整地址，如 http://192.168.1.10:5679/refresh
  - self._webhook_token 鉴权 token（可选）

全程防御式：任何失败都不影响转存主流程。兼容 MoviePilot V2 / V3。
"""
import urllib.parse
import urllib.request
from typing import Optional

try:
    from app.sdk.logging import logger
except (ImportError, ModuleNotFoundError):
    try:
        from app.log import logger
    except (ImportError, ModuleNotFoundError):
        import logging
        logger = logging.getLogger(__name__)


def trigger_refresh(plugin, path_hint: str = "") -> bool:
    """POST strm-gen webhook 触发媒体库刷新。成功发出返回 True。"""
    url = str(getattr(plugin, "_webhook_url", "") or "").strip()
    if not url:
        return False
    token = str(getattr(plugin, "_webhook_token", "") or "").strip()
    try:
        # 附加 path 提示（仅用于 strm-gen 日志），保留已有 query
        sep = "&" if "?" in url else "?"
        if path_hint:
            url = url + sep + "path=" + urllib.parse.quote(path_hint[:60])
        if token and "token=" not in url:
            url = url + ("&" if "?" in url else "?") + "token=" + urllib.parse.quote(token)
        headers = {"User-Agent": "godfather-transfer/1.0"}
        if token:
            headers["X-Webhook-Token"] = token
        req = urllib.request.Request(url, data=b"", method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            code = getattr(resp, "status", 200)
        try:
            logger.info(f"教父刷新：已触发 strm-gen webhook（HTTP {code}）")
        except Exception:
            pass
        return True
    except Exception as e:
        try:
            logger.warning(f"教父刷新：webhook 触发失败（不影响转存）: {e}")
        except Exception:
            pass
        return False
