"""单 URL xhs video 真 e2e 验证 (6a6fa5f4).

应走 XiaohongshuResolver dispatch → VideoResolver → yt-dlp video download.
"""
import json
import sys
import time
import urllib.request
import http.client
from pathlib import Path
from urllib.parse import urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = "http://127.0.0.1:8001"
URL = "https://www.xiaohongshu.com/explore/6a6fa5f40000000022030ebb?xsec_token=ABcLbNFwAk9szCL5ljWOmztMzEUvAVPLWyyzIYwa4HL1U=&xsec_source=pc_feed"
DOWNLOAD_DIR = Path(r"D:\XiaoProject\Multimedia Parsing\downloads_xhs_video")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


def http_post_json(url, body, timeout=30):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get_sse(url, max_events=50, timeout=300):
    events = []
    p = urlparse(url)
    conn = http.client.HTTPConnection(p.hostname, p.port, timeout=timeout)
    sock = conn.sock
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
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                block, buf = buf.split(b"\n\n", 1)
                line_buf = block.decode("utf-8", errors="replace")
                event_type = None
                data_lines = []
                for line in line_buf.split("\n"):
                    if line.startswith("event: "):
                        event_type = line[7:].strip()
                    elif line.startswith("data: "):
                        data_lines.append(line[6:])
                if data_lines:
                    try:
                        data = json.loads("\n".join(data_lines).strip())
                    except Exception:
                        data = {"raw": "\n".join(data_lines).strip()}
                    events.append({"event": event_type, "data": data})
                    if event_type == "done":
                        return events
    finally:
        conn.close()
    return events


def main():
    print(f"=== xhs video e2e: {URL[:80]}... ===\n")
    print(f"下载目录: {DOWNLOAD_DIR}\n")

    # 1) parse
    r = http_post_json(f"{BASE}/parse", {"urls": [URL]}, timeout=15)
    parse_batch = r["batch_id"]
    print(f"[1/parse] batch_id={parse_batch} url_count={r['url_count']}")
    events = http_get_sse(f"{BASE}/events/{parse_batch}", timeout=120)

    items = []
    for ev in events:
        d = ev.get("data", {})
        if d.get("type") == "resource_fetch_status" and d.get("status") == "success":
            for it in d.get("items", []):
                items.append(it)
        if d.get("status") == "done":
            print(f"  parse summary: {d.get('summary', {})}")
            break

    print(f"  parse 产出 {len(items)} items:")
    for it in items:
        print(f"    - item_id={it.get('item_id')} resource_type={it.get('resource_type')} platform={it.get('platform')}")
        meta = it.get("meta", {})
        if meta.get("video_id"):
            print(f"      video_id={meta.get('video_id')} duration={meta.get('duration')}")
        if meta.get("image_url"):
            print(f"      image_url={meta.get('image_url')[:80]}")

    if not items:
        print("\n❌ parse 0 item, 不跑 fetch")
        return 1

    # 2) fetch
    fetch_items = []
    for it in items:
        fetch_items.append({
            "url": it.get("source_url", ""),
            "source_url": it.get("source_url", ""),
            "resource_type": it.get("resource_type", ""),
            "item_id": it.get("item_id", ""),
            "platform": it.get("platform", ""),
            "title": it.get("title", ""),
            "thumbnail": it.get("thumbnail", ""),
            "meta": it.get("meta", {}),
        })

    fetch_body = {"mode": "local", "items": fetch_items, "download_dir": str(DOWNLOAD_DIR)}
    r2 = http_post_json(f"{BASE}/fetch", fetch_body, timeout=15)
    fetch_batch = r2["batch_id"]
    print(f"\n[2/fetch] batch_id={fetch_batch} item_count={r2.get('item_count', len(items))}")
    fetch_events = http_get_sse(f"{BASE}/events/{fetch_batch}", timeout=600)

    print(f"  fetch {len(fetch_events)} events:")
    for ev in fetch_events:
        d = ev.get("data", {})
        st = d.get("status")
        iid = d.get("item_id")
        rt = d.get("resource_type")
        err = (d.get("error") or "")[:200]
        if st in ("processing", "uploading"):
            print(f"     {st} item={iid} rt={rt}")
        elif st in ("success", "failed", "skipped"):
            print(f"     {st} item={iid} rt={rt} err={err!r}")
        if d.get("status") == "done":
            print(f"  fetch summary: {d.get('summary', {})}")
            break

    print(f"\n=== 下载目录 {DOWNLOAD_DIR} ===")
    for p in sorted(DOWNLOAD_DIR.iterdir()):
        if p.is_file():
            sz = p.stat().st_size
            print(f"  {p.name}  ({sz:,} bytes, {sz/1024/1024:.2f} MB)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
