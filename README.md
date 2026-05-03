# MewkoTV

一个用 Python + FastAPI + SQLite 写的轻量影视聚合站雏形，参考 MoonTV 的核心体验：多源搜索、详情页、在线播放、本地继续观看，以及多视频源测速优选。

## 运行

```powershell
uv sync
uv run python app.py
```

打开 `http://127.0.0.1:8000`。

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

需要持久化的文件很少：保留 `config.json` 和 `data/rikka_tv.sqlite3` 即可。Docker 部署时建议把 `data/` 挂载成卷，否则容器重建后播放记录和缓存会丢失。

如需关闭 `python app.py` 的热重载：

```powershell
$env:MEWKOTV_RELOAD="0"; uv run python app.py
```

## 配置视频源

编辑 `config.json` 的 `api_site`。只接入你有权使用的 MacCMS API：

```json
"api_site": {
  "my_source": {
    "name": "我的视频源",
    "api": "https://your-domain.example/api.php/provide/vod",
    "disabled": false
  }
}
```

`speed_test` 控制多源测速。播放页会先使用站内历史源评分推荐播放源并直接播放；点击“测速”后才会对当前影片的候选源做全量测速，默认按下载速度 70%、清晰度 20%、延迟 10% 综合评分，用户可以再手动切换源。测速结果会写入 SQLite，用于后续源推荐。

视频播放地址默认由浏览器直连；HLS 播放会经后端代理 m3u8 播放列表以重写路径并过滤明显插入广告，但不代理 ts/mp4 等视频片段流量。

## 数据

SQLite 默认写入 `data/rikka_tv.sqlite3`，包含：

- `source_metrics`

本项目不内置可用资源站，不存储视频文件。
