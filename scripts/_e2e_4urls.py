"""一次性跑 4 URL 验证服务 (用户实机部署真测)."""
import json
import sys
import time
import urllib.request
from typing import Any, Dict, List

# Force UTF-8 output on Windows (avoid GBK encode error for emoji)
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = "http://127.0.0.1:8001"
URLS = [
    ("xhs1", "https://www.xiaohongshu.com/explore/6a7a72910000000029032c9b?xsec_token=ABr6OndMqOZkz_CExBSs5qoUhMyGMZkUfiSuRQPyHQ4BU=&xsec_source=pc_feed"),
    ("xhs2", "https://www.xiaohongshu.com/explore/6a741cd8000000002701e8a1?xsec_token=AB56McEZppg29i3MCNfYIUITFKJrXHrBMyBSmULv2kc4U=&xsec_source=pc_feed"),
    ("bilibili", "https://www.bilibili.com/video/BV1jw4m1Y7vM/?spm_id_from=333.1391.0.0&vd_source=603e01dcfb983869335d5a1ffacad76e"),
    ("youtube", "https://www.youtube.com/shorts/xJyk4Yht6Z4"),
]


def http_post_json(url: str, body: dict, timeout: int = 30) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get_json(url: str, timeout: int = 10) -> Any:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get_sse(url: str, max_events: int = 20, timeout: int = 60) -> List[dict]:
    """SSE 客户端 — 拉最多 max_events 个 event, 或直到 'done' / 'ready' 收到."""
    import http.client
    import re

    events: List[dict] = []
    from urllib.parse import urlparse

    p = urlparse(url)
    conn = http.client.HTTPConnection(p.hostname, p.port, timeout=timeout)
    try:
        conn.request("GET", p.path, headers={"Accept": "text/event-stream"})
        resp = conn.getresponse()
        buf = b""
        deadline = time.time() + timeout
        while time.time() < deadline and len(events) < max_events:
            chunk = resp.read(1)
            if not chunk:
                break
            buf += chunk
            if buf.endswith(b"\n\n"):
                line_buf = buf.decode("utf-8", errors="replace").rstrip("\n")
                buf = b""
                event_type = None
                data_lines: List[str] = []
                for line in line_buf.split("\n"):
                    if line.startswith("event: "):
                        event_type = line[7:].strip()
                    elif line.startswith("data: "):
                        data_lines.append(line[6:])
                if data_lines:
                    data_str = "\n".join(data_lines).strip()
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        data = {"raw": data_str}
                    events.append({"event": event_type, "data": data})
                    if event_type == "done" or (isinstance(data, dict) and data.get("type") == "resource_fetch_status" and data.get("status") == "done"):
                        break
    finally:
        conn.close()
    return events


def main() -> int:
    print(f"=== 4 URL e2e ({BASE}) ===\n")

    # 1) POST /parse
    body = {"urls": [u for _, u in URLS]}
    r = http_post_json(f"{BASE}/parse", body, timeout=15)
    batch_id = r["batch_id"]
    print(f"[parse] batch_id={batch_id} url_count={r['url_count']}\n")

    # 2) SSE 拉事件
    events = http_get_sse(f"{BASE}/events/{batch_id}", max_events=30, timeout=120)

    # 3) 按 URL 归类
    by_url: Dict[str, List[dict]] = {label: [] for label, _ in URLS}
    for ev in events:
        d = ev.get("data", {})
        src = d.get("source_url")
        if not src:
            continue
        for label, u in URLS:
            if src.startswith(u.split("?")[0]):  # match by path, ignore query
                by_url[label].append(d)
                break

    # 4) 输出每 URL 结果
    print("=== 结果 ===\n")
    for label, u in URLS:
        evs = by_url[label]
        if not evs:
            print(f"[NONE] {label}: 无事件 (URL: {u[:80]}...)")
            continue
        # 找 final status (success / failed / skipped)
        final = next((e for e in reversed(evs) if e.get("status") in ("success", "failed", "skipped", "done")), None)
        if not final:
            final = evs[-1]
        status = final.get("status", "?")
        rt = final.get("resource_type", "?")
        items = final.get("items", [])
        err = final.get("error") or final.get("summary", {}).get("failed")
        marker = "OK" if status == "success" else "SKIP" if status == "skipped" else "FAIL"
        print(f"[{marker}] {label}: {u[:80]}...")
        print(f"   status={status} resource_type={rt} items={len(items) if items else 0}")
        if err:
            print(f"   err={err}")
        if items:
            for it in items[:3]:
                print(f"   - item_id={it.get('item_id')} platform={it.get('platform')} title={it.get('title', '')[:50]}")
                meta = it.get("meta", {})
                if meta.get("image_url"):
                    print(f"     image_url={meta['image_url'][:120]}")
                if meta.get("video_id"):
                    print(f"     video_id={meta['video_id']}")
        print()

    # 5) 整批 summary
    summary = next((e.get("summary") for e in reversed(events) if e.get("type") == "resource_fetch_status" and e.get("summary")), None)
    if summary:
        print(f"=== 总结: {summary} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
