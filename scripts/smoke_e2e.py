"""scripts/smoke_e2e.py — 端到端 smoke 测试.

启 uvicorn server (后台) → 调 HTTP API (parse + cancel) → 收 SSE events → 验证.

注意: 这个脚本调真 yt-dlp 解析 xhs URL (跟同事要测的场景一致), 跑
1-2 分钟. 同事可以拿这个当 onboarding 测试: 启 server → 跑这个 → 看
events 流是不是符合预期。

用法: python scripts/smoke_e2e.py [--port 8765]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

# 加项目根到 sys.path, 让 `python scripts/smoke_e2e.py` 也能找到 multimedia_parsing
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

DEFAULT_BASE_URL = "http://127.0.0.1:8765"
# 拿 1 个真实 xhs URL 试 parse (不会真下载, 只 parse metadata).
# 同事可以改这个 URL 测试其他平台.
TEST_URLS = [
    "https://www.bilibili.com/video/BV1xx411c7mD",
]


def _post_json(base_url: str, path: str, body: Dict[str, Any], timeout: float = 5.0) -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_sse(base_url: str, path: str, max_events: int = 50, timeout: float = 60.0) -> List[Dict[str, Any]]:
    """GET SSE stream, 收 events 直到 done event 或 max_events."""
    req = urllib.request.Request(f"{base_url}{path}")
    events: List[Dict[str, Any]] = []
    last_event = "message"
    deadline = time.time() + timeout
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # SSE 是 text/event-stream, 逐行读
        for raw_line in resp:
            line = raw_line.decode("utf-8").rstrip("\n").rstrip("\r")
            if time.time() > deadline:
                print(f"[smoke] timeout {timeout}s, 收 {len(events)} events 后退出", file=sys.stderr)
                break
            if not line:
                continue
            if line.startswith(":"):
                continue  # SSE comment
            if ":" in line:
                key, _, val = line.partition(":")
                val = val.lstrip(" ")
                if key == "event":
                    last_event = val
                elif key == "data":
                    try:
                        payload = json.loads(val)
                    except json.JSONDecodeError:
                        continue
                    events.append({"event": last_event, "data": payload})
                    last_event = "message"
                    if payload.get("status") == "done" or last_event == "done":
                        break
                    if len(events) >= max_events:
                        print(f"[smoke] max_events={max_events} 达到, 提前退出", file=sys.stderr)
                        break
    return events


def _check_health(base_url: str) -> None:
    """先 GET /health, 验证 server 在跑."""
    req = urllib.request.Request(f"{base_url}/health")
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    print(f"[smoke] /health ok: version={body['version']}, active_batches={body['active_batches']}")


def smoke_test_parse(base_url: str) -> int:
    """跑 parse smoke test. 返 0 = success, 1 = failure."""
    _check_health(base_url)
    print(f"[smoke] 启动 parse batch, urls={TEST_URLS}")
    r = _post_json(base_url, "/parse", {"urls": TEST_URLS})
    batch_id = r["batch_id"]
    print(f"[smoke] parse batch_id={batch_id}, url_count={r['url_count']}")
    # 收 SSE stream
    print(f"[smoke] 收 SSE events...")
    events = _get_sse(base_url, f"/events/{batch_id}", timeout=90.0)
    print(f"[smoke] 收 {len(events)} events:")
    for ev in events:
        event_type = ev.get("event", "?")
        payload = ev.get("data", {})
        status = payload.get("status", "-")
        stage = payload.get("stage", "-")
        print(f"  - [{event_type:10}] {stage:8} {status:10}", end="")
        if "source_url" in payload:
            print(f"  url={payload['source_url'][:50]}", end="")
        if "error" in payload:
            print(f"  err={payload['error'][:50]}", end="")
        if "summary" in payload:
            print(f"  summary={payload['summary']}", end="")
        print()
    # 至少 1 个 processing + 1 个 success/skipped + 1 个 done
    statuses = [e["data"].get("status") for e in events if e["data"].get("status")]
    if "processing" not in statuses:
        print(f"[smoke] FAIL: 期望 'processing' 事件, 实际 {statuses}")
        return 1
    if not any(s in statuses for s in ("success", "failed", "skipped")):
        print(f"[smoke] FAIL: 期望 success/failed/skipped 之一, 实际 {statuses}")
        return 1
    if "done" not in [e.get("event") for e in events] and "done" not in statuses:
        print(f"[smoke] FAIL: 期望 'done' 事件收尾, 实际 events={[e.get('event') for e in events]}")
        return 1
    # 验证 /batches 看到 batch
    req = urllib.request.Request(f"{base_url}/batches")
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        batches = json.loads(resp.read().decode("utf-8"))
    if not any(b["batch_id"] == batch_id for b in batches):
        print(f"[smoke] FAIL: /batches 找不到 {batch_id}")
        return 1
    print(f"[smoke] PASS: parse batch {batch_id} smoke test ok")
    return 0


def smoke_test_cancel(base_url: str) -> int:
    """跑 cancel smoke test. 启动慢 batch, 立刻 cancel."""
    # 用 b23.tv (短链) + bilibili 真 URL 让 yt-dlp 真的花时间解析
    slow_url = "https://www.bilibili.com/video/BV1xx411c7mD"
    print(f"[smoke] 启动慢 parse batch, url={slow_url}")
    r = _post_json(base_url, "/parse", {"urls": [slow_url]})
    batch_id = r["batch_id"]
    # 立刻 cancel
    time.sleep(0.1)
    print(f"[smoke] 立刻 cancel batch_id={batch_id}")
    cancel_r = _post_json(base_url, f"/cancel/{batch_id}", {})
    if not cancel_r["cancelled"]:
        print(f"[smoke] FAIL: cancel 返 {cancel_r}")
        return 1
    print(f"[smoke] cancel ok: {cancel_r}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Multimedia Parsing e2e smoke test")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="server base url")
    parser.add_argument("--skip-parse", action="store_true", help="跳过真 parse (只测 cancel)")
    args = parser.parse_args()
    if args.skip_parse:
        return smoke_test_cancel(args.base_url)
    # 1. parse
    r1 = smoke_test_parse(args.base_url)
    if r1 != 0:
        return r1
    # 2. cancel
    r2 = smoke_test_cancel(args.base_url)
    return r2


if __name__ == "__main__":
    sys.exit(main())
