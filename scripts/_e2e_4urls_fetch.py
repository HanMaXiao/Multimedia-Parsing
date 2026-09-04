"""完整 e2e: 4 URL 解析 + 真下载到 local dir (验证"拉取就能用"整链路).

输出每个 URL 的 local_path 绝对路径.
"""
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

# Force UTF-8 output on Windows
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = "http://127.0.0.1:8001"
DOWNLOAD_DIR = Path(r"D:\XiaoProject\Multimedia Parsing\downloads_e2e_4urls_v3")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

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


def http_get_sse(url: str, max_events: int = 50, timeout: int = 300) -> List[dict]:
    """SSE 客户端 — 拉 events 直到 status='done' 或 max_events."""
    import http.client
    import socket
    from urllib.parse import urlparse

    events: List[dict] = []
    p = urlparse(url)
    conn = http.client.HTTPConnection(p.hostname, p.port, timeout=timeout)
    sock = conn.sock  # raw socket for read1
    if sock is None:
        conn.connect()
        sock = conn.sock
    sock.settimeout(timeout)
    try:
        conn.request("GET", p.path, headers={"Accept": "text/event-stream"})
        resp = conn.getresponse()
        buf = b""
        deadline = time.time() + timeout
        while time.time() < deadline and len(events) < max_events:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                block, buf = buf.split(b"\n\n", 1)
                line_buf = block.decode("utf-8", errors="replace")
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
                    if event_type == "done":
                        return events
    finally:
        conn.close()
    return events


def main() -> int:
    print(f"=== 4 URL 完整 e2e (parse + fetch local) ===\n")
    print(f"下载目录: {DOWNLOAD_DIR}\n")

    # ---- Step 1: parse ----
    body = {"urls": [u for _, u in URLS]}
    r = http_post_json(f"{BASE}/parse", body, timeout=15)
    parse_batch = r["batch_id"]
    print(f"[1/parse] batch_id={parse_batch} url_count={r['url_count']}")

    events = http_get_sse(f"{BASE}/events/{parse_batch}", timeout=180)

    # 收集所有 success item (resource_type 决定 fetch 时走哪个 resolver)
    items: List[dict] = []
    by_url_status: Dict[str, str] = {}
    for ev in events:
        d = ev.get("data", {})
        if d.get("type") == "resource_fetch_status" and d.get("status") == "success":
            for it in d.get("items", []):
                items.append(it)
            src = d.get("source_url", "")
            for label, u in URLS:
                if src.startswith(u.split("?")[0]):
                    by_url_status[label] = "success"
                    break
        elif d.get("type") == "resource_fetch_status" and d.get("status") in ("skipped", "failed"):
            src = d.get("source_url", "")
            for label, u in URLS:
                if src.startswith(u.split("?")[0]):
                    by_url_status[label] = d.get("status")
                    break

    print(f"  parse 产出 {len(items)} items, 状态: {by_url_status}\n")

    if not items:
        print("❌ parse 阶段 0 item, 不跑 fetch")
        return 1

    # ---- Step 2: fetch (local mode) ----
    # ResourceItem (parse 产出) -> ResourceFetchItem (fetch 要求) 字段转换
    fetch_items = []
    for it in items:
        fetch_items.append({
            "url": it.get("source_url", ""),         # fetch 需要 url 字段
            "source_url": it.get("source_url", ""),
            "resource_type": it.get("resource_type", ""),
            "item_id": it.get("item_id", ""),
            "platform": it.get("platform", ""),
            "title": it.get("title", ""),
            "thumbnail": it.get("thumbnail", ""),
            "meta": it.get("meta", {}),
        })

    fetch_body = {
        "mode": "local",
        "items": fetch_items,
        "download_dir": str(DOWNLOAD_DIR),
    }
    r2 = http_post_json(f"{BASE}/fetch", fetch_body, timeout=15)
    fetch_batch = r2["batch_id"]
    print(f"[2/fetch] batch_id={fetch_batch} item_count={r2.get('item_count', len(items))}")

    fetch_events = http_get_sse(f"{BASE}/events/{fetch_batch}", timeout=600)
    # DEBUG: 把 fetch 阶段每个 event 的关键字段打出来
    print(f"\n  [debug] {len(fetch_events)} fetch events:")
    for i, ev in enumerate(fetch_events):
        d = ev.get("data", {})
        st = d.get("status")
        iid = d.get("item_id")
        plat = d.get("platform")
        rt = d.get("resource_type")
        err = (d.get("error") or "")[:150]
        lp = d.get("local_path")
        if st in ("processing", "uploading") or err:
            print(f"     [{i:02d}] {st} item={iid} plat={plat} rt={rt} err={err!r}")
        elif st in ("success", "failed", "skipped") and iid:
            print(f"     [{i:02d}] {st} item={iid} plat={plat} rt={rt} local_path={lp} err={err!r}")

    # 收集 fetch 完成的 item → local_path
    fetch_results: Dict[str, dict] = {}  # item_id -> FetchResult
    for ev in fetch_events:
        d = ev.get("data", {})
        # fetch 阶段 event 包含 item_id + local_path + error 等
        if d.get("type") == "resource_fetch_status" and d.get("status") in ("success", "failed", "skipped"):
            for it in d.get("items", []):
                fetch_results[it.get("item_id", "")] = it
        # done 阶段 summary
        if d.get("status") == "done":
            print(f"  fetch summary: {d.get('summary', {})}")
            break
        # fetch 阶段也有 per-item resource_fetch_status event (单 item 的 status)
        if d.get("status") in ("uploading", "success", "failed", "skipped") and d.get("item_id"):
            fetch_results[d["item_id"]] = {
                "item_id": d.get("item_id"),
                "resource_type": d.get("resource_type"),
                "platform": d.get("platform"),
                "local_path": d.get("local_path"),
                "oss_url": d.get("oss_url"),
                "error": d.get("error"),
            }

    # ---- Step 3: 列文件 ----
    print(f"\n=== 下载文件清单 ===\n")
    files_found: List[Path] = []
    for it in items:
        item_id = it.get("item_id", "?")
        platform = it.get("platform", "?")
        rt = it.get("resource_type", "?")
        result = fetch_results.get(item_id, {})
        local_path = result.get("local_path")
        oss_url = result.get("oss_url")
        err = result.get("error")
        status = "OK" if local_path else "FAIL"
        print(f"[{status}] item_id={item_id} platform={platform} type={rt}")
        if local_path:
            p = Path(local_path)
            sz = p.stat().st_size if p.exists() else 0
            print(f"   local_path: {p}")
            print(f"   size: {sz:,} bytes ({sz/1024/1024:.2f} MB)" if sz > 1024*1024 else f"   size: {sz:,} bytes")
            files_found.append(p)
        if oss_url:
            print(f"   oss_url: {oss_url}")
        if err:
            print(f"   error: {err}")
        if not local_path and not err:
            # 还没收到 fetch status, 看 SSE 原始 events
            related = [ev for ev in fetch_events if ev.get("data", {}).get("item_id") == item_id]
            if related:
                print(f"   raw_events_for_item:")
                for ev in related[:3]:
                    ed = ev.get("data", {})
                    print(f"     status={ed.get('status')} error={ed.get('error')}")
        print()

    print(f"=== 总计: {len(files_found)}/{len(items)} 文件下载成功 ===")
    if files_found:
        print(f"\n所有下载文件位置:\n")
        for p in files_found:
            print(f"  {p}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
