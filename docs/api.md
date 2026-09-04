# API 文档 — Multimedia Parsing Service

> Base URL: `http://127.0.0.1:8765`（默认）
> Content-Type: `application/json`（SSE 端点除外）
> 交互式文档: 启动服务后访问 `/docs`（Swagger UI，自动生成）

## 端点总览

| Method | Path | 用途 | 成功状态码 |
|---|---|---|---|
| `GET` | `/health` | 健康检查 | 200 |
| `POST` | `/parse` | 启动解析批，返 `{batch_id}` | 202 |
| `POST` | `/fetch` | 启动下载/上传批（mode=local/oss/both） | 202 |
| `POST` | `/cancel/{batch_id}` | 取消批 | 200 |
| `GET` | `/events/{batch_id}` | SSE 事件流 | 200 |
| `GET` | `/batches` | 列出已知 batch 状态 | 200 |

通用错误码：

| 状态码 | 场景 |
|---|---|
| `409` | `batch_id` 已存在 |
| `404` | batch 不存在（cancel / events） |
| `422` | 字段校验失败（extra=forbid，多余字段也会 422） |

---

## GET /health

```bash
curl http://127.0.0.1:8765/health
```

```json
{ "status": "ok", "version": "0.1.0", "active_batches": 2 }
```

`active_batches` = state 为 `running` 的 batch 数。

---

## POST /parse

启动解析批。**立即返回**，结果通过 SSE 流获取。

```bash
curl -X POST http://127.0.0.1:8765/parse \
  -H "Content-Type: application/json" \
  -d '{"urls": ["https://www.bilibili.com/video/BV1xx411c7mD"]}'
```

Request body：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `urls` | `string[]` (≥1) | ✅ | 待解析 URL，必须 http(s) |
| `batch_id` | `string` | ❌ | 自定义 ID；缺省自动生成 `parse_{unix_ms}_{hex8}` |

Response `202`：

```json
{ "batch_id": "parse_1725450000000_a1b2c3d4", "status": "accepted", "url_count": 1 }
```

---

## POST /fetch

启动下载/上传批。同样立即返回，进度走 SSE。

```bash
curl -X POST http://127.0.0.1:8765/fetch \
  -H "Content-Type: application/json" \
  -d '{
    "mode": "local",
    "download_dir": "C:/Users/you/Downloads",
    "items": [{
      "url": "https://www.bilibili.com/video/BV1xx",
      "source_url": "https://www.bilibili.com/video/BV1xx",
      "resource_type": "video",
      "item_id": "bilibili_BV1xx",
      "platform": "bilibili"
    }]
  }'
```

Request body：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `mode` | `"local" \| "oss" \| "both"` | ✅ | 3-mode 下载（D3） |
| `items` | `ResourceFetchItem[]` (≥1) | ✅ | 见下表 |
| `download_dir` | `string` | mode=local/both 必填 | 本地下载目录 |
| `oss` | `OssConfig` | mode=oss/both 必填 | 见下表 |
| `batch_id` | `string` | ❌ | 缺省自动生成 `fetch_{unix_ms}_{hex8}` |
| `cookie_file` | `string` | ❌ | Netscape cookies.txt 路径（video 登录态） |
| `account_id` | `string` | ❌ | 可选，账号标识 |

**mode 互斥校验**（D3）：

- `local` → `download_dir` 必填，`oss` 必须为 null
- `oss` → `oss` 必填，`download_dir` 必须为 null
- `both` → 两者都必填（OSS 失败自动降级为本地）

**ResourceFetchItem**：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `url` | `string` | ✅ | 资源 URL（video=源页，image=直链），http(s) |
| `source_url` | `string` | ✅ | 用户原始粘贴 URL，http(s) |
| `resource_type` | `"video" \| "image" \| "audio" \| "model"` | ✅ | audio/model 为预留类型 |
| `item_id` | `string` | ✅ | `{platform}_{native_id}`，OSS key 依赖它 |
| `platform` | `string` | ✅ | `bilibili` / `xiaohongshu` / `weibo` / ... |
| `title` | `string` | ❌ | 资源标题 |
| `thumbnail` | `string` | ❌ | 缩略图 URL |
| `meta` | `object` | ❌ | resolver 扩展字段（image: width/height 等） |

**OssConfig**：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `provider` | `string` | ✅ | `aliyun_oss` / `tencent_cos` / `cloudflare_r2` / `minio` / `custom` |
| `endpoint` | `string` | ✅ | S3 兼容 endpoint |
| `region` | `string` | ✅ | 区域 |
| `bucket` | `string` | ✅ | 桶名 |
| `access_key_id` | `string` | ✅ | AK |
| `secret_access_key` | `string` | ✅ | SK |
| `path_prefix` | `string` | ❌ | key 前缀；OSS key 规则 `{path_prefix}/{yyyy-MM-dd}/{item_id}.{ext}` |
| `public_base_url` | `string` | ❌ | 公开直链前缀；缺省推导 `https://{bucket}.{endpoint_host}/{key}` |

Response `202`：

```json
{ "batch_id": "fetch_1725450000000_e5f6a7b8", "status": "accepted", "item_count": 1 }
```

---

## POST /cancel/{batch_id}

协作式取消 — set `threading.Event`，run_parse / run_fetch 在每个 item 处理前检查。

```bash
curl -X POST http://127.0.0.1:8765/cancel/parse_1725450000000_a1b2c3d4
```

```json
{ "batch_id": "parse_1725450000000_a1b2c3d4", "cancelled": true }
```

- batch 不存在 → `404`
- 已取消/已完成 → `{ "cancelled": false, "reason": "already cancelled or done" }`

---

## GET /events/{batch_id}

SSE 事件流（`text/event-stream`）。连接建立先发 `ready`，batch 结束发 `done` 后关闭；中途 cancel / crash 也会正常收尾。

```bash
curl -N http://127.0.0.1:8765/events/parse_1725450000000_a1b2c3d4
```

```text
event: ready
data: {"batch_id": "parse_...", "ts": "2026-09-04T15:30:00Z"}

data: {"type": "resource_fetch_status", "batch_id": "...", "stage": "parse",
       "status": "processing", "source_url": "...", "ts": "..."}

data: {"type": "resource_fetch_status", "batch_id": "...", "stage": "parse",
       "status": "success", "source_url": "...", "items": [...], "ts": "..."}

event: done
data: {"batch_id": "...", "ts": "..."}
```

**事件 payload 公共字段**：`batch_id` / `ts` / `entry_id`（= batch_id，Phase 48 兼容）。

**status 九态机**（SPEC §3.3）：

| 类别 | 值 |
|---|---|
| 非终态 | `pending` / `logging_in` / `uploading` / `processing` |
| 终态 | `success` / `draft` / `failed` / `skipped` / `cancelled` |

**batch 级收尾事件**：runner 结束时发一条 `status=done`，带 `summary: {total, success, failed, skipped}`。

Header 说明：响应带 `Cache-Control: no-cache` + `X-Accel-Buffering: no`（禁 nginx 缓冲）。

---

## GET /batches

列出已知 batch（按 `started_at` 倒序）。**单进程内存态** — 重启后清空。

```bash
curl http://127.0.0.1:8765/batches
```

```json
[{
  "batch_id": "parse_1725450000000_a1b2c3d4",
  "stage": "parse",
  "total": 1, "done": 1, "success": 1, "failed": 0,
  "state": "done",
  "started_at": "2026-09-04T15:30:00Z",
  "updated_at": "2026-09-04T15:30:12Z"
}]
```

| 字段 | 说明 |
|---|---|
| `state` | `running` / `done` |
| `done` | 已出终态的 item 数（`skipped` 不计入 done） |
| `total` | item 总数（progress 事件持续校准） |

---

## 已验证（Gate）

| 验证项 | 结果 |
|---|---|
| `pytest -m "not slow"` | 463 passed / 0 failed（含 10 个新 server tests） |
| uvicorn 启动 | OK |
| `/health` | `{status: ok, version: 0.1.0}` |
| `/parse` 真 uvicorn | `202 Accepted`，返 batch_id |
| `/cancel/{batch_id}` | 取消成功 |
| `/batches` | 显示 batch 列表（state=done, success=1） |

> 完整 parse smoke test（真站点 + cookie）见 [ROADMAP.md](../ROADMAP.md) P0。
