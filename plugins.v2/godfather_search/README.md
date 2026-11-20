# 教父资源转存插件（MoviePilot V3 / V2 双兼容）

输入「影片名？」搜索教父站资源，自动完成：

1. **优先 115 网盘资源** → 有则直接转存到你的 115 网盘；
2. **无 115 时识别磁力资源** → 按「画质 + 做种数」加权排序 → 取最优磁力，用 115 离线下载转存；
3. 消息返回每一步的决策依据与执行结果，可回复编号指定资源。

参照 [`hqyel/moviepilot-plugins-third`](https://github.com/hqyel/moviepilot-plugins-third) 的插件结构开发，同时提供 V3 原生实现与 V2 兼容实现。

---

## 一、目录结构

```
godfather_search/                      ← 仓库根目录（作为插件源）
├── package.v3.json                    ← V3 插件市场索引（V3 宿主读取）
├── package.v2.json                    ← V2 插件市场索引（V2 宿主读取）
├── icons/
│   └── godfather.png
├── plugins.v3/                        ← MoviePilot V3（3.x）用
│   └── godfather_search/
│       ├── __init__.py                ← 主类：V3 SDK 优先、V2 旧路径回退
│       ├── godfather_client.py        ← 教父站搜索（Cookie + PoW）
│       ├── pan115_client.py           ← 115：分享转存 + 磁力离线下载
│       ├── resource_parser.py         ← 资源解析 + 画质/做种识别 + 加权排序
│       ├── pyproject.toml             ← V3 依赖清单
│       └── README.md
└── plugins.v2/                        ← MoviePilot V2 用（同样内容，requirements.txt）
    └── godfather_search/
```

> 同一个仓库地址可同时服务 V3 与 V2：MoviePilot 按自身版本读取对应索引与目录，无需区分填写。

---

## 二、安装到 MoviePilot V3（当前主推）

### 方式 A：作为插件源安装（推荐）

1. 先把本目录推送到你自己的 GitHub 仓库的 `main` 分支（插件市场只读取 `main` 分支）。
2. MoviePilot Web → **设定 → 插件 → 插件市场设置（齿轮图标）**。
3. 在「插件仓库地址」/ `PLUGIN_MARKET` 中加入你的仓库地址，例如：
   ```
   https://github.com/你的用户名/你的仓库名
   ```
   多个地址用英文逗号 `,` 分隔。
4. 点击**同步插件源**，在插件市场搜索「教父资源转存」→ 安装。
5. 进入插件详情，按下方「配置」填写 Cookie，打开启用开关。

> 也可以用环境变量 `PLUGIN_MARKET=https://github.com/你的用户名/你的仓库名` 配置后重启容器。

### 方式 B：手动复制安装（无需 GitHub）

1. 找到 MoviePilot 的**配置目录**（Docker 通常是挂载到容器的 `/config`）。
2. 把 `plugins.v3/godfather_search/` 整个文件夹复制到配置目录下的 `plugins/` 文件夹中，最终形如：
   ```
   /config/plugins/godfather_search/__init__.py
   ```
3. 在容器内安装依赖（如宿主机已装可跳过）：
   ```bash
   pip install beautifulsoup4 requests playwright
   playwright install chromium   # 用于自动完成站点 PoW 浏览器验证
   ```
4. 重启 MoviePilot，在 **设定 → 插件** 中找到「教父资源转存」，配置并启用。

> 如果你的 MoviePilot 是 Docker，进入容器可用：
> `docker exec -it moviepilot bash`（容器名按实际替换）。

### 兼容性说明

- 本插件导入层做了 **V3/V2 双兼容**：V3 优先用 `app.sdk.*`，V2 自动回退到 `app.plugins` 等旧路径。
- 若你仍在使用 **MoviePilot V2**，把 `plugins.v2/godfather_search/` 复制到配置目录 `plugins/` 即可，用法相同。

### Python 依赖

插件市场会按 `pyproject.toml`（V3）/ `requirements.txt`（V2）自动安装。手动安装时执行：

```bash
pip install beautifulsoup4 requests playwright
playwright install chromium   # 如需浏览器自动验证 PoW
```

---

## 三、配置（插件设置页）

| 配置项 | 说明 |
| --- | --- |
| **启用插件** | 打开开关 |
| **教父站地址** | 默认 `https://www.xn--wcv59z.com`（即 www.教父.com 的 punycode） |
| **教父站 Cookie** | 必填。浏览器登录教父站后，从开发者工具复制完整 Cookie |
| **浏览器验证(PoW)** | 建议开启。站点有人机验证时，用内置浏览器自动完成计算 |
| **115 网盘 Cookie** | 必填。115 登录后复制，需含 `UID/CID/SEID/KID` 等字段 |
| **115 目标目录 ID** | `0` 表示根目录 |
| **或按路径转存** | 如 `媒体库/电影`，填了则优先按路径查找/创建目录 |
| **画质权重 / 做种权重** | 磁力排序的加权系数，默认各 1.0 |
| **展示条数** | 结果列表展示前 N 条，默认 8 |
| **触发后缀** | 默认中文问号 `？`，英文 `?` 同样生效 |

### 如何获取 Cookie

- **教父站**：Chrome 登录 → F12 → Network → 刷新页面 → 任选请求 → Request Headers → 复制整行 `Cookie` 值。
- **115 网盘**：同理，登录 115.com 后复制 `Cookie`（确认包含 `UID`、`CID`、`SEID`、`KID`）。

> Cookie 会过期。失效后重新登录网站并更新配置即可。

---

## 四、使用

在任意消息界面（微信 / Telegram / App 等）：

```
武林外传？        ← 搜索并自动转存最优资源
复仇者联盟？
2？               ← 指定转存上一条结果里的第 2 个资源
```

**自动决策逻辑：**

- 结果里有 115 网盘资源 → 转存画质最高的那个；
- 没有 115 → 对磁力按 `画质分 × 画质权重 + log10(做种数+1)×30 × 做种权重` 排序，取最高分那个提交 115 离线下载。

**画质识别：** 4K/2160p、1080p/蓝光、720p、标清，并对 REMUX/HDR/x265/10bit/杜比等加分。

> 与其他监听用户消息的插件（如 ChatGPT 插件）可能冲突，需调整插件优先级，把本插件排前面。

---

## 五、合规说明

- 插件**不破解登录鉴权、不打码、不撞库**。所有登录态由用户自己的 Cookie 提供，站点的人机验证仅用真实浏览器环境正常完成。
- 插件**不收集、不上传**用户 Cookie 到任何第三方；Cookie 仅本地用于访问你本人账号。
- 请确保你有权访问相关内容，并遵守所在地区法律法规及站点服务条款。作者不对使用者的内容访问行为承担责任。

---

## 六、接口说明（供二次开发）

| 能力 | 接口 |
| --- | --- |
| 教父站搜索 | `GET {site_url}/search?q=<关键词>` （HTML，可用 Playwright 过 PoW） |
| 115 分享转存 | `POST https://webapi.115.com/share/receive` (share_code, receive_code, cid, is_check=0) |
| 115 磁力离线 | `POST https://115.com/web/lixian/?ct=lixian&ac=add_task_url` (url, wp_path_id) |
| 115 目录 | `GET webapi.115.com/files/search` / `POST webapi.115.com/files/add` |

若教父站前端结构变化导致解析不到资源，通常只需调整 `resource_parser.py` 中的链接正则（`RE_115` / `RE_MAGNET`）与画质/做种正则，无需改动主流程。

---

## 七、版本

- **v1.0.0**：首个版本。教父站搜索 + 115 优先转存 + 磁力画质/做种排序离线下载。
