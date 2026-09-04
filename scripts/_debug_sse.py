"""debug: 拉 fetch batch_id 的所有 events 看 error."""
import json
import time
import http.client
import sys
from urllib.parse import urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

batch = sys.argv[1] if len(sys.argv) > 1 else "fetch_1788512518261_f30690c5"
url = f"http://127.0.0.1:8001/events/{batch}"
p = urlparse(url)
conn = http.client.HTTPConnection(p.hostname, p.port, timeout=30)
conn.request("GET", p.path, headers={"Accept": "text/event-stream"})
resp = conn.getresponse()
print(f"status: {resp.status}")
buf = b""
events = []
deadline = time.time() + 15
while time.time() < deadline and len(events) < 80:
    chunk = resp.read(1)
    if not chunk:
        break
    buf += chunk
    if buf.endswith(b"\n\n"):
        line_buf = buf.decode("utf-8", errors="replace").rstrip("\n")
        buf = b""
        for line in line_buf.split("\n"):
            if line.startswith("data: "):
                try:
                    d = json.loads(line[6:])
                    events.append(d)
                except Exception:
                    pass
conn.close()
print(f"got {len(events)} events\n")
for i, e in enumerate(events):
    status = e.get("status")
    typ = e.get("type")
    iid = e.get("item_id")
    err = str(e.get("error") or "")[:200]
    plat = e.get("platform")
    rt = e.get("resource_type")
    print(f"[{i:02d}] status={status} type={typ} item_id={iid} platform={plat} rt={rt}")
    if err:
        print(f"     error: {err}")
