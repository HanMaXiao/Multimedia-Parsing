"""multimedia_parsing.server — FastAPI HTTP service.

API 端点 (详细见 docs/api.md):
  - GET  /health                  → 健康检查
  - POST /parse                   → 启动解析, 返 {batch_id}
  - POST /fetch                   → 启动下载/上传, 返 {batch_id}
  - POST /cancel/{batch_id}       → 取消批
  - GET  /events/{batch_id}       → SSE 事件流
  - GET  /batches                  → 列出已知 batch

设计:
  - run_parse / run_fetch 是 sync, 在后台 thread 跑
  - 每个 batch_id 配 1 个 asyncio.Queue, sync thread 调 sync callback
    push event → async loop 消费 → SSE emit
  - cancel 走 threading.Event (run_parse / run_fetch 内部检查)
  - Pydantic v2 models 镜像 resource_fetcher dataclasses
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, List, Literal, Optional

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .event_emitter import now_iso
from .resource_fetcher.base import (
    FetchDestination,
    FetchResult,
    ResourceItem,
    ResourceType,
)
from .resource_fetcher.manifest import (
    ResourceFetchItem,
    ResourceFetchManifest,
    ResourceParseManifest,
)
from .resource_fetcher.run_fetch import run_fetch
from .resource_fetcher.run_parse import run_parse
from .oss.oss_uploader import OssConfig

# FetchMode = "local" / "oss" / "both" (D3 决策)
# Python 端不在 base.py 暴露 enum, 用 Literal + FetchDestination 互斥校验
FetchMode = Literal["local", "oss", "both"]

__version__ = "0.1.0"

# ---------------------------------------------------------------------------
# Batch state (per-process, in-memory)
# ---------------------------------------------------------------------------

# 每个 batch 一个 asyncio.Queue, sync run_parse/run_fetch callback push event
_batch_queues: Dict[str, asyncio.Queue] = {}
# cancel threading.Event, run_parse/run_fetch 内部检查
_batch_cancel_events: Dict[str, threading.Event] = {}
# batch 状态 (stage / total / done / state)
_batch_status: Dict[str, Dict[str, Any]] = {}
# background thread refs (让 GC 不回收)
_batch_threads: Dict[str, threading.Thread] = {}


def _enqueue_event(batch_id: str, event: Dict[str, Any]) -> None:
    """sync callback: push event 到 batch 的 asyncio.Queue.

    run_parse / run_fetch 在后台 thread 调, push event → main async loop
    pop → SSE emit.

    注: Queue 是 thread-safe (asyncio.Queue.put_nowait 是 atomic), 不需要锁.
    """
    q = _batch_queues.get(batch_id)
    if q is None:
        # batch 已被 GC / cancel, drop event (正常 — cancel 后还有 race 期间 emit)
        return
    try:
        q.put_nowait(event)
    except asyncio.QueueFull:
        # Queue 满 (1MB limit), drop. 生产中可通过调大 limit 缓解.
        pass


def _build_sync_event_cb(batch_id: str):
    """构造 run_parse / run_fetch 的 sync event_cb.

    把 emit event dict push 到 asyncio.Queue, 同时更新 _batch_status 聚合.
    """
    def cb(payload: Dict[str, Any]) -> None:
        # 补 entry_id (Phase 48 兼容字段, 服务端用 batch_id 替代)
        payload.setdefault("entry_id", batch_id)
        payload["batch_id"] = batch_id
        payload.setdefault("ts", now_iso())
        # 聚合到 _batch_status
        _update_batch_status(batch_id, payload)
        # push 到 SSE queue
        _enqueue_event(batch_id, payload)
    return cb


def _update_batch_status(batch_id: str, payload: Dict[str, Any]) -> None:
    """聚合 event 到 _batch_status (供 GET /batches 列表用)."""
    status = _batch_status.setdefault(batch_id, {
        "batch_id": batch_id,
        "stage": payload.get("stage", "unknown"),
        "total": 0,
        "done": 0,
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "state": "running",  # running / done
        "started_at": now_iso(),
        "updated_at": now_iso(),
    })
    status["updated_at"] = now_iso()
    if payload.get("status") == "success":
        status["done"] += 1
        status["success"] += 1
    elif payload.get("status") in ("failed", "cancelled"):
        status["done"] += 1
        status["failed"] += 1
    elif payload.get("status") == "skipped":
        # skipped 不算 done
        pass
    # progress field 推算 total
    if "progress" in payload and isinstance(payload["progress"], dict):
        status["total"] = max(status["total"], payload["progress"].get("total", 0))


# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------


class ParseRequest(BaseModel):
    """POST /parse request body."""
    model_config = ConfigDict(extra="forbid")
    urls: List[str] = Field(..., min_length=1, description="待解析 URL 列表 (1+ 条)")
    batch_id: Optional[str] = Field(None, description="自定义 batch_id; None 时自动生成")


class ParseResponse(BaseModel):
    """POST /parse response body."""
    batch_id: str
    status: str = "accepted"
    url_count: int


class FetchRequest(BaseModel):
    """POST /fetch request body."""
    model_config = ConfigDict(extra="forbid")
    mode: FetchMode = Field(..., description="local / oss / both (D3)")
    items: List[ResourceFetchItem] = Field(..., min_length=1, description="待下载 items (1+ 条)")
    download_dir: Optional[str] = Field(None, description="mode=local/both 必填")
    batch_id: Optional[str] = None
    cookie_file: Optional[str] = Field(None, description="Netscape cookies.txt 路径 (video auth)")
    account_id: Optional[str] = None
    oss: Optional[OssConfig] = Field(None, description="mode=oss/both 必填")


class FetchResponse(BaseModel):
    batch_id: str
    status: str = "accepted"
    item_count: int


class CancelResponse(BaseModel):
    batch_id: str
    cancelled: bool
    reason: Optional[str] = None


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    active_batches: int


class BatchStatus(BaseModel):
    batch_id: str
    stage: str
    total: int
    done: int
    success: int
    failed: int
    state: str
    started_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# Background runners
# ---------------------------------------------------------------------------


def _run_parse_in_thread(batch_id: str, manifest: ResourceParseManifest) -> None:
    """后台 thread 跑 run_parse, 用 cancel_event 检查取消."""
    cancel_event = _batch_cancel_events[batch_id]
    try:
        # run_parse 串行处理 manifest.urls, 每个 URL 一轮 processing + 终态
        results = run_parse(
            manifest,
            event_cb=_build_sync_event_cb(batch_id),
            cancel_event=cancel_event,
        )
        # 收尾: 标记 batch done
        _batch_status.setdefault(batch_id, {})["state"] = "done"
        _enqueue_event(batch_id, {
            "type": "resource_fetch_status",
            "batch_id": batch_id,
            "stage": "parse",
            "status": "done",
            "ts": now_iso(),
            "summary": {
                "total": len(results),
                "success": sum(1 for r in results if r.error is None and len(r.items) > 0),
                "failed": sum(1 for r in results if r.error is not None),
                "skipped": sum(1 for r in results if r.error is None and len(r.items) == 0),
            },
        })
    except Exception as exc:
        _enqueue_event(batch_id, {
            "type": "resource_fetch_status",
            "batch_id": batch_id,
            "stage": "parse",
            "status": "failed",
            "error": f"parse runner crashed: {exc}",
            "ts": now_iso(),
        })
        _batch_status[batch_id]["state"] = "done"
    finally:
        # 标记 Queue 终止 (SSE 端点收到 sentinel 后退出)
        _enqueue_event(batch_id, None)  # None = 终止 sentinel


def _run_fetch_in_thread(batch_id: str, manifest: ResourceFetchManifest) -> None:
    cancel_event = _batch_cancel_events[batch_id]
    try:
        results = run_fetch(
            manifest,
            event_cb=_build_sync_event_cb(batch_id),
            cancel_event=cancel_event,
        )
        _batch_status.setdefault(batch_id, {})["state"] = "done"
        _enqueue_event(batch_id, {
            "type": "resource_fetch_status",
            "batch_id": batch_id,
            "stage": "fetch",
            "status": "done",
            "ts": now_iso(),
            "summary": {
                "total": len(results),
                "success": sum(1 for r in results if r.is_success),
                "failed": sum(1 for r in results if not r.is_success),
            },
        })
    except Exception as exc:
        _enqueue_event(batch_id, {
            "type": "resource_fetch_status",
            "batch_id": batch_id,
            "stage": "fetch",
            "status": "failed",
            "error": f"fetch runner crashed: {exc}",
            "ts": now_iso(),
        })
        _batch_status[batch_id]["state"] = "done"
    finally:
        _enqueue_event(batch_id, None)  # sentinel


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # startup: nothing
    yield
    # shutdown: 等待所有 batch thread 退出 (避免 zombie)
    for t in list(_batch_threads.values()):
        t.join(timeout=5.0)


app = FastAPI(
    title="Multimedia Parsing Service",
    version=__version__,
    description=(
        "Universal multimedia resource parsing service — video / image / "
        "(audio, model) smart URL routing + 3-mode download (local / oss / both) "
        "with SSE event stream."
    ),
    lifespan=_lifespan,
)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        version=__version__,
        active_batches=sum(1 for s in _batch_status.values() if s.get("state") == "running"),
    )


@app.post("/parse", response_model=ParseResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_parse(req: ParseRequest) -> ParseResponse:
    """启动解析批 — 立即返 batch_id, SSE 流通过 GET /events/{batch_id} 收."""
    batch_id = req.batch_id or f"parse_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    if batch_id in _batch_queues:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"batch_id {batch_id!r} already exists",
        )
    # 构造 manifest (用 Pydantic 校验, 顺便做 http(s) 校验)
    try:
        manifest = ResourceParseManifest(batch_id=batch_id, urls=req.urls)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"invalid manifest: {exc}",
        )
    # 初始化 per-batch 状态
    _batch_queues[batch_id] = asyncio.Queue(maxsize=10000)
    _batch_cancel_events[batch_id] = threading.Event()
    _batch_status[batch_id] = {
        "batch_id": batch_id,
        "stage": "parse",
        "total": len(req.urls),
        "done": 0,
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "state": "running",
        "started_at": now_iso(),
        "updated_at": now_iso(),
    }
    # 启动后台 thread
    t = threading.Thread(
        target=_run_parse_in_thread,
        args=(batch_id, manifest),
        name=f"parse-{batch_id}",
        daemon=True,
    )
    _batch_threads[batch_id] = t
    t.start()
    return ParseResponse(batch_id=batch_id, url_count=len(req.urls))


@app.post("/fetch", response_model=FetchResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_fetch(req: FetchRequest) -> FetchResponse:
    """启动下载/上传批 — mode 校验 (D3 互斥)."""
    batch_id = req.batch_id or f"fetch_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    if batch_id in _batch_queues:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"batch_id {batch_id!r} already exists",
        )
    # mode 互斥校验 (D3)
    if req.mode == "local" and not req.download_dir:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="download_dir is required when mode='local'",
        )
    if req.mode == "oss" and req.oss is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="oss is required when mode='oss'",
        )
    if req.mode == "both" and (not req.download_dir or req.oss is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="both download_dir and oss are required when mode='both'",
        )
    # 构造 FetchDestination (Pydantic 校验模式互斥, 走 dataclass 内部)
    try:
        from pathlib import Path
        dest = FetchDestination(
            mode=req.mode,
            download_dir=Path(req.download_dir) if req.download_dir else None,
            oss=req.oss,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"invalid dest: {exc}",
        )
    # 构造 manifest
    manifest = ResourceFetchManifest(
        batch_id=batch_id,
        mode=req.mode,
        items=req.items,
        download_dir=Path(req.download_dir) if req.download_dir else None,
        oss=req.oss,
        cookie_file=Path(req.cookie_file) if req.cookie_file else None,
        account_id=req.account_id,
    )
    # 初始化
    _batch_queues[batch_id] = asyncio.Queue(maxsize=10000)
    _batch_cancel_events[batch_id] = threading.Event()
    _batch_status[batch_id] = {
        "batch_id": batch_id,
        "stage": "fetch",
        "total": len(req.items),
        "done": 0,
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "state": "running",
        "started_at": now_iso(),
        "updated_at": now_iso(),
    }
    t = threading.Thread(
        target=_run_fetch_in_thread,
        args=(batch_id, manifest),
        name=f"fetch-{batch_id}",
        daemon=True,
    )
    _batch_threads[batch_id] = t
    t.start()
    return FetchResponse(batch_id=batch_id, item_count=len(req.items))


@app.post("/cancel/{batch_id}", response_model=CancelResponse)
async def cancel_batch(batch_id: str) -> CancelResponse:
    """取消批 — set threading.Event, run_parse / run_fetch 内部检查."""
    cancel_event = _batch_cancel_events.get(batch_id)
    if cancel_event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no such batch: {batch_id!r}",
        )
    if cancel_event.is_set():
        return CancelResponse(batch_id=batch_id, cancelled=False, reason="already cancelled or done")
    cancel_event.set()
    return CancelResponse(batch_id=batch_id, cancelled=True)


@app.get("/events/{batch_id}")
async def stream_events(batch_id: str) -> StreamingResponse:
    """SSE 事件流 (text/event-stream). 收 batch_id 走完或 cancel / 出错 → 关闭."""
    q = _batch_queues.get(batch_id)
    if q is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no such batch: {batch_id!r}",
        )

    async def gen() -> AsyncGenerator[str, None]:
        # 立即发 1 个 ready 事件, 让 client 知道连接已建立
        yield f"event: ready\ndata: {json.dumps({'batch_id': batch_id, 'ts': now_iso()})}\n\n"
        while True:
            event = await q.get()
            if event is None:
                # 终止 sentinel
                yield f"event: done\ndata: {json.dumps({'batch_id': batch_id, 'ts': now_iso()})}\n\n"
                break
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 禁用 nginx 缓冲
        },
    )


@app.get("/batches", response_model=List[BatchStatus])
async def list_batches() -> List[BatchStatus]:
    """列出已知 batch 状态."""
    return [
        BatchStatus(**s)
        for s in sorted(
            _batch_status.values(),
            key=lambda x: x.get("started_at", ""),
            reverse=True,
        )
    ]


# ---------------------------------------------------------------------------
# Entry point (供 [project.scripts] console_script)
# ---------------------------------------------------------------------------


def main() -> None:
    """Console entry: `multimedia-parsing-server --host 0.0.0.0 --port 8765`."""
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(
        prog="multimedia-parsing-server",
        description="Universal multimedia resource parsing service (FastAPI + SSE).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload (dev)")
    parser.add_argument("--workers", type=int, default=1, help="Worker count (default: 1, 单进程)")
    args = parser.parse_args()
    uvicorn.run(
        "multimedia_parsing.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
