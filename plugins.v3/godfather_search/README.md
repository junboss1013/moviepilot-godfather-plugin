# 教父资源转存插件（MoviePilot V3 / V2 双兼容）

输入「影片名？」搜索教父站资源，自动完成：

1. 搜索教父站 → 进入影片详情页，从 `/res/downurl` 数据接口提取全部资源；
2. 按「画质 + 做种数」加权排序 → 取最优磁力，用 **115 离线下载**转存到你的网盘；
3. 消息返回排序结果与执行结果，可回复编号指定资源。

> **关于「115 优先」**：经实测，教父站的资源本质都是磁力（网盘分类走 keepshare 离线中转），**站内没有 `115.com/s/` 分享直链**，因此无法做「115 分享直接转存」。插件保留了对 115 分享直链的识别能力（若站点将来提供），当前实际走的是「磁力 → 115 离线下载」这条路，已端到端验证可用。

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
│       ├── godfather_client.py        ← 教父站搜索（账号密码自动登录 + PoW）
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
5. 进入插件详情，按下方「配置」填写**教父站账号 + 密码**与 115 Cookie，打开启用开关。

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

### 常见问题：浏览器报 `Executable doesn't exist` / `chrome-headless-shell`

搜索时若报 `BrowserType.launch: Executable doesn't exist at ...chromium_headless_shell...`，说明 **Playwright 库装了但浏览器二进制没下载全**（MoviePilot 官方镜像常见）。插件 **v3.2.3 起会在浏览器缺失时自动下载安装并重试**（首次约需 1-2 分钟，稍后重发片名即可）；若自动安装也失败，再进容器手动补装：

```bash
# 进入 MoviePilot 容器（容器名按实际替换）
docker exec -it moviepilot bash

# 安装完整 Chromium（推荐，一次到位）
playwright install chromium

# 若提示 playwright 命令不存在，用 python 模块方式：
python -m playwright install chromium

# 空间有限时，也可只装无头版：
python -m playwright install chromium-headless-shell
```

装完 **重启 MoviePilot** 再试。若容器里的 playwright 由插件自动安装在独立 venv，需在该 venv 环境下执行上述命令（可先 `which python` 确认）。

> 插件会在报错消息里直接附上这些安装命令，按提示操作即可。

---

## 三、配置（插件设置页）

| 配置项 | 说明 |
| --- | --- |
| **启用插件** | 打开开关 |
| **教父站地址** | 默认 `https://www.xn--wcv59z.com`（即 www.教父.com 的 punycode） |
| **教父站账号 \*** | 必填。教父站登录用户名 / 邮箱，插件会自动登录 |
| **教父站密码 \*** | 必填。教父站登录密码（密码框显示，仅保存在本机插件配置） |
| **浏览器验证(PoW)** | 建议开启。站点有人机验证时，用内置浏览器自动完成计算 |
| **115 网盘 Cookie** | 必填。115 登录后复制，需含 `UID/CID/SEID/KID` 等字段 |
| **转存时自动分类** | 默认开启。按电影/电视剧+年份+季在 115 网盘自动建目录（见下方） |
| **分类根目录(115网盘内)** | 默认 `影视库`，分类目录的根 |
| **电影目录名 / 电视剧目录名** | 默认 `电影` / `电视剧`，Emby 分别添加这两个文件夹 |
| **画质权重 / 做种权重** | 磁力排序的加权系数，默认各 1.0 |
| **展示条数** | 结果列表展示前 N 条，默认 8 |
| **触发后缀** | 默认中文问号 `？`，英文 `?` 同样生效 |

### 教父站登录说明

- 插件用你填写的**账号 + 密码**，通过内置浏览器自动访问 `/user/login` 登录（表单字段 `username` / `password`），无需手动复制 Cookie。已端到端验证：自动登录后可正常搜索并解析出资源。
- 登录态与 PoW 验证保留在同一个浏览器上下文，因此每次搜索都是一次完整的自动登录流程，稳定可靠。
- 兼容旧配置：若仍保留了 `site_cookie` 字段（开发者配置），插件会优先用账号密码，账号密码为空时回退到 Cookie。

### 如何获取 115 Cookie

- **115 网盘**：登录 115.com 后 F12 → Network → 刷新 → 任选请求 → Request Headers → 复制 `Cookie`（确认包含 `UID`、`CID`、`SEID`、`KID`）。

> 115 Cookie 会过期，失效后重新登录并更新配置即可。教父站用账号密码则无需关心 Cookie 过期。

### 转存自动分类（Emby 分别刮削电影/电视剧）

开启「转存时自动分类」后，插件在提交转存前会先识别资源类型，并在 115 网盘自动建好分类目录，磁力/分享直接落到对应目录：

```
影视库/
├── 电影/
│   └── 电影名 (年份)/                 ← 如 肖申克的救赎 (1994)
└── 电视剧/
    └── 剧集名称 (年份)/               ← 如 权力的游戏 (2011)
        ├── Season 01/
        │   ├── 剧集名称 S01E01 - 集名.mkv   ← 文件名保留种子原名，Emby 从 TMDB 刮削
        │   └── 剧集名称 S01E02 - 集名.mkv
        └── Season 02/
```

- **文件夹命名**：电影总目录 `电影名 (年份)`，电视剧总目录 `剧集名称 (年份)`，季目录 `Season XX`。年份从种子标题提取（如标题含年份则带上，没有则只用片名）。
- **文件名**：保持种子下载的原文件名不改动，由 Emby 从 TMDB 自动识别刮削出片名、集名、海报等元数据（标准命名如 `剧名 S01E01.xxx` 即可正确刮削）。
- 类型识别：标题含 `S01E01` / `第01集` / `全24集` / `Season 2` / `第二季` 判为电视剧，否则电影；片名优先取你搜索的关键词。
- 电视剧能识别季号时自动建 `Season XX` 子目录；识别不到则只建到「剧集名称 (年份)」一级，由 Emby 按文件名自行分季。
- **配合 Emby**：用 rclone / openlist 把 115 挂载为本地目录后，添加两个媒体库——电影库指向 `影视库/电影`，电视剧库指向 `影视库/电视剧`（TV 显示类型），即可分别刮削。
- 关闭自动分类时，转存到 115 网盘根目录（`0`），不再需要手动填写目标目录 ID 或路径。

---

## 四、使用

在任意消息界面（微信 / Telegram / App 等）：

```
武林外传？        ← 搜索并自动转存最优资源
复仇者联盟？
2？               ← 指定转存上一条结果里的第 2 个资源
```

**自动决策逻辑：**

- 对资源按 `画质分 × 画质权重 + log10(做种数+1)×30 × 做种权重` 排序，取最高分那个提交 115 离线下载；
- 若将来站点出现 115 分享直链，则优先转存画质最高的 115 资源（代码已保留该分支）。

**画质识别：** 采用站点画质代码映射（i14=4K / i8=1080P / i2=720P / i6=原盘等），并结合标题中的 4K/2160p/1080p/720p、中字、REMUX/HDR/x265/10bit/杜比等关键词加权。

> 与其他监听用户消息的插件（如 ChatGPT 插件）可能冲突，需调整插件优先级，把本插件排前面。

---

## 五、合规说明

- 插件**不破解登录鉴权、不打码、不撞库**。教父站用用户本人的账号密码在其自有账号上自动登录，115 用用户自己的 Cookie；站点的人机验证仅用真实浏览器环境正常完成。
- 插件**不收集、不上传**用户账号、密码、Cookie 到任何第三方；这些凭据仅本地用于访问你本人账号。
- 请确保你有权访问相关内容，并遵守所在地区法律法规及站点服务条款。作者不对使用者的内容访问行为承担责任。

---

## 六、接口说明（供二次开发）

| 能力 | 接口 |
| --- | --- |
| 教父站搜索 | `GET {site_url}/search?q=<关键词>` → 提取影片详情链接 `/mv/xxxx` |
| 教父站自动登录 | `GET {site_url}/user/login` → 填 `username`/`password` → `button[type=submit]` 提交，浏览器上下文保留登录态 |
| 教父站资源数据 | `GET {site_url}/res/downurl/mv/xxxx` → JSON 列式数组（m=btih / t=标题 / e=做种 / p=画质代码 / s=大小） |
| PoW 验证 | 站点 `/res/pow`，由内置浏览器自动完成（纯 requests 会被拦截） |
| 115 分享转存 | `POST https://webapi.115.com/share/receive` (share_code, receive_code, cid, is_check=0) |
| 115 磁力离线 | `POST https://115.com/web/lixian/?ct=lixian&ac=add_task_url` (url, wp_path_id) |
| 115 目录 | `GET webapi.115.com/files/search` / `POST webapi.115.com/files/add` |

若教父站前端结构变化导致解析不到资源，通常只需调整 `resource_parser.py` 中的 `parse_downlist_json()` 字段映射与画质代码表（`QUALITY_CODE_MAP`），无需改动主流程。

---

## 七、版本

- **v3.3.2 / v1.3.2**：移除「115 目标目录 ID」「或按路径转存」两个手动配置项——转存目录完全由自动分类决定（关闭分类则存根目录），配置更简洁。
- **v3.3.1 / v1.3.1**：电视剧总目录统一加上年份（`剧名 (年份)/Season XX`）；文件名保留种子原名，由 Emby 从 TMDB 刮削。
- **v3.3.0 / v1.3.0**：新增**转存自动分类**——按电影/电视剧、年份、季在 115 网盘自动建目录，配合 rclone/openlist 挂载后 Emby 可按电影/电视剧分别刮削（识别逻辑本地可独立测试，转存链路已 mock 端到端验证）。
- **v3.2.3 / v1.2.3**：浏览器二进制完全缺失时，插件自动调用 `playwright install` 下载无头浏览器并重试启动（已模拟缺失环境端到端验证）；仍失败时报错附手动安装命令。
- **v3.2.2 / v1.2.2**：修复做种数为 -1（站点未知值）时排序报 `math.log10 domain error` 导致搜索崩溃的问题；已在真实 MoviePilot v3.2.9 实例联动验证（解析 129 条资源、浏览器自动登录正常）。
- **v3.2.1 / v1.2.1**：修复浏览器启动报错 `Executable doesn't exist ...chrome-headless-shell`——内置多策略自动回退（完整 Chromium channel → 系统 Chrome 路径 → 默认无头版），并把安装命令写进报错提示。
- **v3.2.0 / v1.2.0**：教父站登录方式改为**填写账号 + 密码自动登录**（内置浏览器填表提交 `/user/login`，字段 `username`/`password`），无需再手动复制 Cookie；保留 Cookie 直填作为兼容回退；已用真实账号端到端验证（自动登录 + 解析 128 条资源）。
- **v3.1.0 / v1.1.0**：实测适配真实站点结构——详情页 `/res/downurl` JSON 列式解析、画质代码映射、做种加权排序、115 离线下载闭环；修复 PoW 等待超时与 Cookie 注入卡死问题；已端到端验证。
- **v3.0.0 / v1.0.0**：初始版本（基于早期假设的搜索页直链解析，实际站点结构不同）。
