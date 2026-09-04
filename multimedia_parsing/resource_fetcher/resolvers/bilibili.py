"""resolvers.bilibili — B 站视频解析器 (Phase 7+ 平台特定扩展样板).

权威定义: docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md §4.

设计要点 (同事参考):
  - **继承 VideoResolver**: 复用 video_fetcher (yt-dlp) 解析/下载链路, 不重复实现.
  - **平台特定增强**: parse() 拿到父类 items 后, 给 B 站条目加 platform-specific meta
    (如分P / UP主 / 时长格式化 / 弹幕数等). fetch() 透传给父类, 不特殊化.
  - **何时继承 VideoResolver vs 自己写 ResourceResolver**:
      - 继承: 平台能用 yt-dlp 解析/下载 + 你只想加少量元数据 → 选这个.
      - 自己写: 平台不能用 yt-dlp (反爬严格 / 自有 API) 或跨类型 dispatch
        (类似 XiaohongshuResolver F0.1 终判) → 选自己写, 走 ResourceResolver 协议.

扩展点 (同事按需加):
  - 弹幕数: 走 B 站 https://api.bilibili.com/x/v2/dm/stat?aid={aid} 拿.
  - UP主粉丝: 走 https://api.bilibili.com/x/relation/stat?mid={mid}.
  - 分P: yt-dlp playlist 解析已自动产出多条 ResourceItem, 这里无需额外处理.
  - 字幕: yt-dlp writesubtitles=True 下载时自动落, 不在 parse 阶段.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .video import VideoResolver


class BilibiliResolver(VideoResolver):
    """B 站视频解析器 — 继承 VideoResolver, 加 B 站特定元数据.

    router 注册示例 (router.py):
        ResolverRule("video", ("bilibili.com", "b23.tv"), BilibiliResolver),
    """

    # B 站特定 meta 字段 (前端可消费, 非必需)
    _BILIBILI_META_KEYS = ("uploader", "uploader_id", "view_count", "like_count")

    def parse(self, url: str) -> List[ResourceItem]:
        """解析 B 站 URL → 1..N 条 ResourceItem (分P时 N>1).

        流程:
          1. 调 super().parse() 走 yt-dlp, 拿 base items (含 video_id / duration / extractor).
          2. 对 platform='bilibili' 的 item 增补 B 站特定 meta (如 uploader, view_count).
        """
        items = super().parse(url)
        for item in items:
            if item.platform == "bilibili":
                # 注: 父类 parse() 当前只把 {video_id, duration, extractor} 放进 meta.
                # 同事想拿 uploader/view_count/like_count 等 B 站特定字段, 需扩展
                # VideoResolver.parse() 或在 BilibiliResolver 内部自己调 yt-dlp 拿
                # 完整 info_dict (见基类 _resolve_video 的 video_fetcher.resolver 协议).
                item.meta.setdefault("is_bilibili", True)
        return items


__all__ = ["BilibiliResolver"]
