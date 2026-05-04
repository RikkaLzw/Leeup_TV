# MewkoTV

一个用 Python + FastAPI + SQLite 写的轻量影视聚合站雏形，参考 MoonTV 的核心体验：多源搜索、详情页、在线播放、本地继续观看、搜索进度反馈，以及多视频源测速优选。

本项目不内置可用资源站，不存储视频文件。请只接入你有权使用的 MacCMS API。

## 功能概览

- 多源搜索与结果合并，支持优先搜索历史表现较好的视频源。
- 搜索提交后会显示加载提示、动态进度条和已等待秒数，避免长时间多源搜索时误以为页面无响应。
- 播放页支持历史源评分自动推荐，也支持手动测速换源。
- 手动测速提供 `速度优先` 和 `清晰度优先` 两种排序。
- HLS 测速优先在用户浏览器端解析 m3u8，并对真实视频分片做 Range 采样测速。
- 无法测出真实速度但能确认播放的源会显示 `可播放`，排序低于真实测速结果，也不会写入历史源评分。
- HLS 播放可经后端代理 m3u8 播放列表，用于重写相对路径并过滤明显广告片段；不代理 ts/mp4 等视频片段流量。
- 豆瓣图片默认走浏览器直连 CDN，减轻本站服务器图片流量。
- 支持本地继续观看、跳过片头片尾、投屏入口、TVBox 配置导出。

## 运行

```powershell
uv sync
uv run python app.py
```

打开：

```text
http://127.0.0.1:8000
```

如需关闭 `python app.py` 的热重载：

```powershell
$env:MEWKOTV_RELOAD="0"; uv run python app.py
```

可用环境变量：

```text
MEWKOTV_HOST       默认 0.0.0.0
MEWKOTV_PORT       默认 8000
PORT              部署平台常用端口变量，优先级高于 MEWKOTV_PORT
MEWKOTV_RELOAD     0/false/no/off 可关闭热重载
RIKKA_SECRET_KEY   Session 密钥，生产环境建议设置
```

## 轻量部署

生产环境不要上传 `.venv`。如果希望最省资源，1Panel 运行目录指向项目根目录后，可以使用：

```bash
pip install --no-cache-dir -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000 --workers 1
```

如果希望更新代码后自动重启，使用项目入口启动即可，默认开启热重载：

```bash
pip install --no-cache-dir -r requirements.txt
python app.py
```

如果用 Docker 部署，仓库内的 `Dockerfile` 已使用 `python:3.12-slim` 和 `--no-cache-dir`，`.dockerignore` 会排除 `.venv`、缓存、数据库等本地文件，避免把无关内容打进镜像。

需要持久化的文件很少：

```text
config.json
data/rikka_tv.sqlite3
```

Docker 部署时建议把 `data/` 挂载成卷，否则容器重建后播放记录、源评分和缓存会丢失。

## 配置视频源

编辑 `config.json` 的 `api_site`。示例：

```json
"api_site": {
  "my_source": {
    "name": "我的视频源",
    "api": "https://your-domain.example/api.php/provide/vod",
    "disabled": false
  }
}
```

字段说明：

- `name`：页面显示的视频源名称。
- `api`：MacCMS 采集接口地址。
- `disabled`：设为 `true` 可临时禁用。
- `detail`：可选，部分 HTML 详情兜底逻辑会用到。

## 搜索体验

普通搜索默认优先使用历史表现较好的源，避免每次都全源搜索。搜索页会显示已搜索源数量；如果还有更多源可查，会出现 `搜索更多源` 按钮。

搜索过程可能受慢源影响。提交搜索后，页面会立即显示：

- 搜索按钮 `搜索中` 状态。
- 搜索进度提示卡片。
- 动态进度条。
- 超过数秒后的慢源等待提示。
- 超过 8 秒后的已等待秒数。

当前进度提示是页面请求级反馈，不是后端逐源百分比。后续如果需要真实逐源进度，可以再把搜索改成流式接口或轮询任务接口。

## 播放优选与测速

`speed_test` 控制多源优选和测速。播放页默认会先根据站内历史源评分尝试推荐更好的播放源；点击测速按钮后才会对当前影片候选源做浏览器端实测。

播放页有两种手动测速排序：

- `速度优先`：先按真实测速速度排序，同速再看清晰度，最后看响应耗时。
- `清晰度优先`：先按探测到的清晰度排序，相同清晰度下再按真实测速速度排序，最后看响应耗时。

HLS 测速流程：

1. 浏览器获取 m3u8。
2. 如果是 master playlist，选择最高分辨率 variant。
3. 获取 media playlist。
4. 抽取真实视频分片。
5. 使用 `fetch + Range` 读取分片前一小段计算速度。
6. 如果 Range 被跨域限制，会退回普通 GET 读取一小段并取消流。
7. 如果仍无法测出速度，再退回 Hls.js 或媒体元素 metadata 探测。

结果含义：

- `1080p · 2.4 MB/s · 820ms`：测到了真实分片下载速度，可参与排序并写入历史源评分。
- `1080p · 可播放 · 1805ms`：只确认可播放，没测到可靠速度；排序低于真实测速结果，不写入历史源评分。
- `测速失败`：该候选源本次不可用或无法探测。

这里显示的毫秒值更接近“测速链路首响应耗时”，不是纯网络 ping，也不是完整首帧时间。

历史源评分会写入 SQLite，用于后续默认推荐、搜索源优先级和解析阶段源排序。当前源评分大致由速度、历史平均测速分和成功率共同决定；速度会受 `browser_speed_cap_kbps` 封顶，默认约 12 MB/s。

## HLS 代理与广告过滤

视频播放地址默认由浏览器直连。开启 `player.hls_proxy_enabled` 后，m3u8 播放列表会经过后端 `/hls-proxy`：

- 重写相对 m3u8 路径为绝对路径。
- 对嵌套 m3u8 继续代理。
- 根据广告标记和关键词过滤明显广告片段。
- 不代理 ts/mp4 等视频片段流量。

`hls_proxy_bypass_hosts` 中的域名会跳过 m3u8 代理，直接交给浏览器播放。

## 豆瓣图片 CDN

`douban.image_proxy_type` 控制豆瓣图片展示方式。默认值：

```json
"douban": {
  "base_url": "https://m.douban.com",
  "timeout_seconds": 10,
  "image_proxy_type": "cmliussss-cdn-ali",
  "image_proxy_url": ""
}
```

常用取值：

- `cmliussss-cdn-ali`：浏览器直连 `img.doubanio.cmliussss.com`。
- `cmliussss-cdn-tencent`：浏览器直连 `img.doubanio.cmliussss.net`。
- `img3`：浏览器直连 `img3.doubanio.com`。
- `server`：经本站 `/image/douban` 转发。
- `custom`：使用 `image_proxy_url` 拼接原图 URL。

CDN 模式下是用户浏览器直接请求 CDN 图片，不再由本站服务器把 CDN 当上游拉图。

## TVBox 接口

默认关闭。开启后会把 `config.json` 里已启用的 MacCMS 源导出为 TVBox 配置。推荐直接在 `config.json` 配：

```json
"tvbox": {
  "enabled": true,
  "password": "leeup"
}
```

也可以用环境变量临时覆盖配置：

```powershell
$env:TVBOX_ENABLED="true"
$env:PASSWORD="leeup"
uv run python app.py
```

订阅地址：

```text
http://127.0.0.1:8000/api/tvbox/config?pwd=leeup
```

部署到 HTTPS 域名后，把域名替换成自己的站点地址即可，例如：

```text
https://tv.example.com/api/tvbox/config?pwd=leeup
```

口令优先读取 `TVBOX_PASSWORD`，未设置时读取 `PASSWORD`。如果没有设置环境变量，则读取 `config.json` 里的 `tvbox.password`。

## 数据

SQLite 默认写入 `data/rikka_tv.sqlite3`。主要包含：

- `source_metrics`：视频源测速与成功率统计。
- `detail_cache`：详情缓存。
- `search_cache`：搜索缓存。
- `source_resolution_cache`：解析源负面缓存。
- `play_resolution_cache`：解析播放缓存。
- `recommend_cache`：推荐内容缓存。
- `visitor_stats`：访客统计。

清理数据库会影响历史源评分、继续观看和缓存命中。

## 维护提示

- 修改静态资源后，应用会根据 CSS/JS 文件修改时间生成资源版本号，浏览器通常会自动刷新缓存。
- 如果发现某些源一直只显示 `可播放`，通常是该源禁止浏览器读取分片、CORS 不完整，或分片响应过慢。
- 如果搜索长期很慢，可以减少源数量，或调低 `speed_test.search_preferred_source_limit` / `manual_search_max_page`。
- 本项目仅提供搜索、记录和播放入口，不提供、不上传、不存储视频内容。
