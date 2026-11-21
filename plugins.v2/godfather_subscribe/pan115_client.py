"""
115 网盘客户端：基于用户自己的 Cookie 调用 115 Web API。

功能：
  1. 分享链接转存（receive_share）：POST webapi.115.com/share/receive
  2. 磁力离线下载（add_offline_task）：POST 115.com/web/lixian/ (ct=lixian&ac=add_task_url)
  3. 离线任务列表 / 查询根目录 ID

合规说明：所有操作均使用用户自行提供的 115 登录 Cookie，
本模块不收集、不存储、不转存任何用户凭据到第三方。
"""
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

import requests

from .resource_parser import parse_115_link


class Pan115Client:
    WEBAPI = "https://webapi.115.com"
    WEB = "https://115.com"
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    def __init__(self, cookie: str, timeout: int = 20, no_proxy: bool = True):
        self.cookie = (cookie or "").strip()
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.UA,
            "Cookie": self.cookie,
            "Referer": "https://115.com/",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        })
        if no_proxy:
            # 115 为国内服务，走系统代理常被拦截；默认禁用代理
            self.session.trust_env = False
            self.session.proxies = {"http": None, "https": None}

    # -------------------------------------------------- 状态检查
    def check(self) -> Tuple[bool, str]:
        """检查 Cookie 是否有效。"""
        if not self.cookie or "=" not in self.cookie:
            return False, "未配置 115 Cookie"
        try:
            resp = self.session.get(
                f"{self.WEBAPI}/user/info",
                timeout=self.timeout,
            )
            data = resp.json()
            # user/info 的 data 可能是字典（含 user_id）或字符串（脱敏手机号）
            info = data.get("data")
            if data.get("state") and (
                (isinstance(info, dict) and (info.get("user_id") or info.get("user_name")))
                or (isinstance(info, str) and info.strip())
            ):
                return True, "115 Cookie 有效"
            # 部分接口 user/info 不可用，用文件列表兜底判断
            resp2 = self.session.get(
                f"{self.WEBAPI}/files",
                params={"cid": "0", "limit": 1},
                timeout=self.timeout,
            )
            d2 = resp2.json()
            if d2.get("state"):
                return True, "115 Cookie 有效"
            return False, f"115 Cookie 无效或已过期: {data.get('error', '') or d2.get('error', '')}"
        except Exception as e:
            return False, f"115 Cookie 校验异常: {e}"

    # -------------------------------------------------- 分享转存
    def receive_share(self, share_link: str, target_cid: str = "0") -> Dict:
        """
        将 115 分享链接转存到自己的网盘。

        :param share_link: https://115.com/s/xxxxxx?password=abcd 形式
        :param target_cid: 目标目录 ID，0 表示根目录
        :return: {"state": bool, "msg": str, ...}
        """
        if not share_link:
            return {"state": False, "msg": "分享链接为空"}
        share_code, receive_code = parse_115_link(share_link)
        if not share_code:
            return {"state": False, "msg": f"无法解析 115 分享链接: {share_link}"}

        payload = {
            "share_code": share_code,
            "receive_code": receive_code,
            "cid": str(target_cid or "0"),
            "is_check": "0",
        }
        try:
            resp = self.session.post(
                f"{self.WEBAPI}/share/receive",
                data=payload,
                timeout=(10, self.timeout),
                headers={"Referer": share_link},
            )
            data = resp.json()
        except Exception as e:
            return {"state": False, "msg": f"转存请求异常: {e}"}

        errno = data.get("errno")
        if data.get("state") is True:
            return {"state": True, "msg": "转存成功", "data": data}
        if errno == 4100024:
            return {"state": True, "msg": "该文件已转存过（幂等）", "data": data}
        if errno in (4100007, 4100008):
            return {"state": False, "msg": "提取码错误或分享已失效", "data": data}
        return {
            "state": False,
            "msg": f"转存失败: {data.get('error') or data.get('msg') or errno or data}",
            "data": data,
        }

    # -------------------------------------------------- 磁力离线下载
    def add_offline_task(self, magnet: str, target_cid: str = "0") -> Dict:
        """
        添加磁力链接到 115 离线下载。

        接口：POST https://115.com/web/lixian/?ct=lixian&ac=add_task_url
        body: url=<magnet>&wp_path_id=<cid>
        """
        if not magnet or not magnet.lower().startswith("magnet:"):
            return {"state": False, "msg": "无效的磁力链接"}
        try:
            resp = self.session.post(
                f"{self.WEB}/web/lixian/",
                params={"ct": "lixian", "ac": "add_task_url"},
                data={"url": magnet, "wp_path_id": str(target_cid or "0")},
                timeout=(10, self.timeout),
                headers={"Referer": "https://115.com/web/lixian/"},
            )
            data = resp.json()
        except Exception as e:
            return {"state": False, "msg": f"离线下载请求异常: {e}"}

        if data.get("state") is True:
            return {
                "state": True,
                "msg": "离线下载任务已添加",
                "info_hash": data.get("info_hash", ""),
                "data": data,
            }
        errcode = data.get("errcode")
        if errcode in (10008, 911):
            # 幂等：磁力已在离线任务队列中，目标已达成，视为成功
            return {
                "state": True,
                "msg": "任务已存在（已在离线队列中，无需重复添加）",
                "idempotent": True,
                "data": data,
            }
        return {
            "state": False,
            "msg": f"离线下载失败: {data.get('error_msg') or data.get('error') or errcode or data}",
            "data": data,
        }

    def list_offline_tasks(self, page: int = 1) -> List[Dict]:
        """列出离线任务，便于查询下载完成状态。"""
        try:
            resp = self.session.get(
                f"{self.WEB}/web/lixian/",
                params={"ct": "lixian", "ac": "task_lists", "page": page},
                timeout=self.timeout,
            )
            return resp.json().get("tasks", [])
        except Exception:
            return []

    # -------------------------------------------------- 目录辅助
    def get_or_create_folder(self, path: str, parent_cid: str = "0") -> Optional[str]:
        """
        按路径逐级查找/创建目录，返回最终目录 ID。
        path 形如 "媒体库/电影"，留空返回 parent_cid。

        注意：115 的 /files/search 对新建目录存在索引延迟，可能搜不到刚建的目录；
        且文件夹的 fc 字段为 0（非文件夹为 >0）。因此这里改用 /files 按父 CID
        列子项查找同名文件夹（fc==0），可靠且不受搜索索引影响。
        """
        path = (path or "").strip().strip("/")
        if not path:
            return parent_cid
        cid = str(parent_cid or "0")

        def _list_folders(parent: str):
            """列出指定目录下的文件夹，返回 {name: cid}。offset 分页以覆盖较多子项。"""
            found = {}
            offset = 0
            page_size = 200
            for _ in range(20):  # 最多翻 20 页，避免极端情况下死循环
                try:
                    resp = self.session.get(
                        f"{self.WEBAPI}/files",
                        params={
                            "cid": parent, "offset": offset,
                            "limit": page_size, "show_dir": 1, "type": 0,
                        },
                        timeout=self.timeout,
                    )
                    data = resp.json().get("data") or []
                except Exception:
                    break
                if not data:
                    break
                for it in data:
                    # fc==0 表示文件夹；同时按 name 精确匹配
                    if it.get("fc") == 0 and it.get("n"):
                        found[str(it["n"])] = str(it.get("cid"))
                if len(data) < page_size:
                    break
                offset += page_size
            return found

        for name in path.split("/"):
            name = name.strip()
            if not name:
                continue
            folders = _list_folders(cid)
            if name in folders:
                cid = folders[name]
                continue
            # 未找到则创建
            try:
                resp2 = self.session.post(
                    f"{self.WEBAPI}/files/add",
                    data={"cname": name, "pid": cid},
                    timeout=self.timeout,
                )
                d2 = resp2.json()
                new_id = d2.get("file_id") or d2.get("cid")
                if d2.get("state") and new_id:
                    cid = str(new_id)
                else:
                    return None
            except Exception:
                return None
        return cid

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass


if __name__ == "__main__":
    import sys
    cookie = sys.argv[1] if len(sys.argv) > 1 else ""
    client = Pan115Client(cookie)
    print(client.check())
