"""debug: 看 yt-dlp 拿 xhs video URL 实际返什么."""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import yt_dlp, json
opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
URL = "https://www.xiaohongshu.com/explore/6a6fa5f40000000022030ebb?xsec_token=ABcLbNFwAk9szCL5ljWOmztMzEUvAVPLWyyzIYwa4HL1U=&xsec_source=pc_feed"
with yt_dlp.YoutubeDL(opts) as ydl:
    info = ydl.extract_info(URL, download=False, process=False)
keys = ["id", "title", "duration", "ext", "vcodec", "acodec", "_type", "entries"]
out = {k: info.get(k) for k in keys if k in info}
# formats 摘要
if info.get("formats"):
    out["formats_count"] = len(info["formats"])
    out["formats_sample"] = [
        {"format_id": f.get("format_id"), "ext": f.get("ext"), "vcodec": f.get("vcodec"),
         "acodec": f.get("acodec"), "url": (f.get("url") or "")[:80]}
        for f in info["formats"][:3]
    ]
# 看顶层所有 keys
out["__all_keys__"] = list(info.keys())
# 看 note_info 相关 (xhs 内部)
for k in ["note", "note_info", "imageList", "video"]:
    if k in info:
        v = info[k]
        if isinstance(v, dict):
            out[k] = {kk: str(vv)[:80] for kk, vv in list(v.items())[:5]}
        elif isinstance(v, list):
            out[k] = [str(x)[:80] for x in v[:3]]
        else:
            out[k] = str(v)[:80]
print(json.dumps(out, indent=2, ensure_ascii=False))
