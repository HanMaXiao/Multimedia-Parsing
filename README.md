# Multimedia Parsing Service

> 通用多媒体资源解析服务: video / image / (audio, model) 智能 URL 路由
> + 3-mode 下载 (local / oss / both) + SSE 事件流。FastAPI 实现。

抽自 `Multi-Platform Automated Release` 项目 (Plan 2026-09-03-universal-resource-fetch),
7 phases 收口后, 作为独立 HTTP 服务运行, 供其他项目调用。

## Quick start

```bash
# 1. Install (含 yt-dlp / gallery-dl / playwright / boto3)
pip install -e ".[parsing,dev]"

# 2. (image_fetcher 需要) 装 playwright 浏览器
playwright install chromium

# 3. Run dev server
multimedia-parsing-server --reload --port 8765

# 4. Smoke test (另开 terminal)
python scripts/smoke_e2e.py
```

## API

| Method | Path | 用途 |
|---|---|---|
| `GET` | `/health` | 健康检查 + 当前活跃 batch 数 |
| `POST` | `/parse` | 启动解析批, 返 `{batch_id}` (立即返, async) |
| `POST` | `/fetch` | 启动下载/上传批, 返 `{batch_id}` (mode=local/oss/both) |
| `POST` | `/cancel/{batch_id}` | 取消批 (set threading.Event) |
| `GET` | `/events/{batch_id}` | SSE 事件流 (`text/event-stream`) |
| `GET` | `/batches` | 列出所有已知 batch 状态 |

### POST /parse

```bash
curl -X POST http://127.0.0.1:8765/parse \
  -H "Content-Type: application/json" \
  -d '{"urls": ["https://www.bilibili.com/video/BV1xx411c7mD"]}'
```

Response (202 Accepted):
```json
{
  "batch_id": "parse_1725450000000_a1b2c3d4",
  "status": "accepted",
  "url_count": 1
}
```

### GET /events/{batch_id} (SSE)

```bash
curl -N http://127.0.0.1:8765/events/parse_1725450000000_a1b2c3d4
```

Stream events (one per line):
```
event: ready
data: {"batch_id": "parse_...", "ts": "2026-09-04T15:30:00Z"}

data: {"type": "resource_fetch_status", "batch_id": "...", "stage": "parse", "status": "processing", "source_url": "...", "ts": "..."}

data: {"type": "resource_fetch_status", "batch_id": "...", "stage": "parse", "status": "success", "source_url": "...", "items": [...]}

event: done
data: {"batch_id": "...", "ts": "..."}
```

### POST /fetch

```bash
curl -X POST http://127.0.0.1:8765/fetch \
  -H "Content-Type: application/json" \
  -d '{
    "mode": "local",
    "items": [{
      "url": "https://www.bilibili.com/video/BV1xx",
      "source_url": "https://www.bilibili.com/video/BV1xx",
      "resource_type": "video",
      "item_id": "bilibili_BV1xx",
      "platform": "bilibili"
    }],
    "download_dir": "C:/Users/you/Downloads"
  }'
```

## Test

```bash
# 单测 (~6s)
pytest -m "not slow"

# 全量 (~40s, 含 slow)
pytest

# e2e smoke (起 server, 调真 yt-dlp 解析 bilibili URL)
python scripts/smoke_e2e.py
```

## Architecture

- **run_parse / run_fetch** 来自原项目 `publisher.resource_fetcher` (Plan 7 phases 收口版本)
- **包装层**: VideoResolver / ImageResolver 包 `video_fetcher` / `image_fetcher`
- **3-mode 下载 (D3)**: `local` / `oss` / `both` (本地 + OSS, OSS 失败降级 spec §9)
- **9 状态机 (SPEC §3.3)**: 4 非终态 + 5 终态, payload 走 SSE event stream
- **cancel via threading.Event**: run_parse / run_fetch 内部检查

## Dependencies

| 类别 | 包 | 用途 |
|---|---|---|
| 服务栈 | `fastapi` / `uvicorn[standard]` / `sse-starlette` | HTTP + SSE |
| 视频 | `yt-dlp` | 视频元数据 + 下载 (1000+ 站点) |
| 图片 | `gallery-dl` / `beautifulsoup4` / `lxml` | 图片下载 (1000+ 站点) + BS4 fallback |
| 渲染 | `playwright` | JS 渲染兜底 (image_fetcher 复杂 SPA) |
| OSS | `boto3` | S3 兼容协议 (AWS / 阿里 / 腾讯 / R2 / MinIO) |
| 视频处理 | `imageio-ffmpeg` | B 站 / YouTube 合并流需要 |
| 图片处理 | `Pillow` | 图片转换 / 压缩 |

详见 `pyproject.toml`。

## License

Proprietary.
