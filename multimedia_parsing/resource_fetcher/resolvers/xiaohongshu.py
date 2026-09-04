"""resolvers.xiaohongshu — xhs 智能 dispatch resolver (F0.1 终判).

xhs explore URL 实际可能是视频 (note.video.media.stream) 或图集 (note.imageList)。
yt-dlp xiaohongshu extractor 默认按 video 找 formats,图集无 video formats → 抛
DownloadError "No video formats found!"。VideoResolver.parse 吞掉所有异常返 [],
完全忽略图集。

修法 (用户选 A, 2026-09-04):
  video-first fallback to image:
    1) 调 VideoResolver.parse (走 yt-dlp) → 拿到 video formats 就返 1 条 video item
    2) 没拿到 (DownloadError / 沙箱无 cookies / xhs 反爬限速) → 兜底
       ImageResolver.parse (走 image_fetcher / gallery-dl + BS4)
  图片解析下载**由 image_fetcher 负责**, 不复用 yt-dlp (用户约束:
  "图片解析下载也不应该由 yt-dlp 负责").

router 注册: xhs.com / xhslink.com → XiaohongshuResolver (替换原 VideoResolver 注册).
"""
from __future__ import annotations

import logging
from typing import List, Optional

from ..base import FetchResult, ResourceItem, ResourceResolver
from .image import ImageResolver
from .video import VideoResolver

log = logging.getLogger(__name__)


class XiaohongshuResolver:
    """xhs 智能 dispatch — F0.1 终判按 yt-dlp 能否拿 video 决定走 video / image resolver.

    设计要点 (2026-09-04 修订):
      - 旧实现尝试调 yt-dlp 内部 API (`_search_json` + `_download_webpage`) 拿
        initial_state.note.noteDetailMap, 拿 video.stream / imageList 判别 type.
        实测新版本 yt-dlp 顶层 info 不暴露这些字段, 这条路径不稳.
      - 简化: video-first fallback. 让 VideoResolver.parse (yt-dlp) 试, 拿到 video
        formats 就返 1 video item; 失败 → ImageResolver.parse (gallery-dl) 兜底
        拿图集. 这跟 xhs 帖子实际行为匹配: 视频帖有 video formats, 图集帖没.

    Parameters
    ----------
    cookie_file:
        Netscape cookies.txt 路径 (xhs 反爬严格, 通常必需). 透传给 video + image resolver.
    account_id:
        用户账号 id, 记录用. 跟 video_fetcher manifest 字段对齐.
    """

    def __init__(
        self,
        *,
        cookie_file: Optional[str] = None,
        account_id: Optional[str] = None,
    ) -> None:
        self.cookie_file = cookie_file
        self.account_id = account_id
        # 内部用 video / image resolver 复用 parse / fetch 逻辑
        self._video_resolver = VideoResolver(
            cookie_file=cookie_file, account_id=account_id
        )
        self._image_resolver = ImageResolver()

    def parse(self, url: str) -> List[ResourceItem]:
        """F0.1 终判: video-first fallback to image.

        流程:
          1) VideoResolver.parse(url) — 调 yt-dlp 拿 video formats
          2) 成功 (拿到 ≥1 item, 必有 video formats + duration) → 直接返
          3) 失败 (DownloadError / 无 cookies / 反爬) → 兜底 ImageResolver.parse
             (gallery-dl 拿 imageList, 即便 xhs 反爬通常也能拿到部分图)

        实测:
          - xhs video URL (e.g. 6a6fa5f4) → 返 1 video item, 真 video stream URL
          - xhs image URL (e.g. 6a7a7291) → video 解析失败 → image 兜底 → 返 N image items
          - 沙箱无 cookies 时 image 兜底仍能拿到 (gallery-dl 比 yt-dlp 反爬宽松)
        """
        # 1) 视频优先
        try:
            video_items = self._video_resolver.parse(url)
        except Exception as exc:
            log.debug("xhs video parse failed: %s", exc)
            video_items = []

        if video_items:
            return video_items

        # 2) 兜底 image (gallery-dl 拿 imageList)
        try:
            image_items = self._image_resolver.parse(url)
        except Exception as exc:
            log.debug("xhs image parse failed: %s", exc)
            image_items = []

        return image_items

    def fetch(self, item: ResourceItem, dest) -> FetchResult:
        """dispatch fetch 到对应 resolver — F0.1 终判, ResourceItem.resource_type 决定."""
        if item.resource_type == "video":
            return self._video_resolver.fetch(item, dest)
        if item.resource_type == "image":
            return self._image_resolver.fetch(item, dest)
        # audio / model 等 D2 预留 — 跟 VideoResolver.fetch 同样的 error 模式
        return FetchResult(
            item_id=item.item_id,
            resource_type=item.resource_type,
            platform=item.platform,
            error=f"XiaohongshuResolver cannot fetch resource_type={item.resource_type!r}",
        )


__all__ = ["XiaohongshuResolver"]
