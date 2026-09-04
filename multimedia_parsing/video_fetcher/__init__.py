"""video_fetcher — 视频链接解析下载 → OSS 子系统(plan 2026-09-02-video-fetch-oss)。

权威规范:
  - docs/superpowers/specs/2026-09-02-video-fetch-oss-design.md
  - docs/superpowers/plans/2026-09-02-video-fetch-oss/{task_plan,findings,progress}.md

模块拆分:
  - ``manifest``              — manifest JSON 解析 + 边界校验
  - ``storage_state_adapter`` — Playwright storage_state JSON → Netscape cookies.txt
  - ``resolver``              — yt-dlp 元数据提取
  - ``downloader``            — yt-dlp 下载 + progress hook
  - ``oss_uploader``          — boto3 S3 兼容上传 + 直链推导(Phase 2)
  - ``run_fetch``             — 逐 URL resolve → download → upload 编排(Phase 2)

事件协议:复用 ``event_emitter.emit_platform_status``,status 走 9 状态白名单,
platform 字段 = 站点名(bilibili 等),cell 行标识 article = ``{platform}_{video_id}``。

D1 决策(2026-09-02):yt-dlp Python 库内嵌 sidecar,拒绝 spawn CLI 子进程。
D3 决策:boto3 S3 兼容多服务商(阿里云 OSS / 腾讯 COS / R2 / MinIO)。
"""

from __future__ import annotations

__all__ = [
    "manifest",
    "storage_state_adapter",
]

