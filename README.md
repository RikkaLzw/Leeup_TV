# LeeupTV

一个用 Python + FastAPI + SQLite 写的轻量影视聚合站雏形，参考 MoonTV 的核心体验：多源搜索、详情页、在线播放、本地继续观看，以及多视频源测速优选。

## 运行

```powershell
uv sync
uv run python app.py
```

打开 `http://127.0.0.1:8000`。

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

视频播放地址默认由浏览器直连，后端不代理视频流量。

## 数据

SQLite 默认写入 `data/rikka_tv.sqlite3`，包含：

- `source_metrics`

本项目不内置可用资源站，不存储视频文件。
