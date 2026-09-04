"""resolvers.youtube — YouTube 视频解析器 (Phase 7+ 平台特定扩展样板).

权威定义: docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md §4.

设计要点 (同事参考):
  - **继承 VideoResolver**: 复用 video_fetcher (yt-dlp) 解析/下载链路.
  - **平台特定增强**: YouTube 有 playlist / 字幕 / 多分辨率 / 章节 (chapters) / 缩略图
    等 yt-dlp 都能拿的字段, 同事可在此 resolver 里取出来加进 meta.
  - **跟 BilibiliResolver 区别**: 结构上一样, 但 YT 登录态 cookie_file 推荐
    (防 YouTube 触发 429 / "Sign in to confirm you're not a bot").

扩展点 (同事按需加):
  - playlist: yt-dlp playlist 解析已自动产出多条 ResourceItem, 父类已 cover.
  - 字幕: yt-dlp writesubtitles=True (download 时), 解析阶段拿 subtitle 列表用 info_dict.
  - 章节: info_dict['chapters'] 拿 [{title, start_time, end_time}].
  - 缩略图: info_dict['thumbnails'] 拿 [{url, width, height}], 选最大进 item.thumbnail.
  - like_count / view_count: info_dict 直接有.
"""
from __future__ import annotations

from typing import List

from .video import VideoResolver


class YouTubeResolver(VideoResolver):
    """YouTube 视频解析器 — 继承 VideoResolver, 加 YouTube 特定元数据.

    router 注册示例 (router.py):
        ResolverRule("video", ("youtube.com", "youtu.be"), YouTubeResolver),

    备注:
      - YouTube 强反爬 (429 / "Sign in"), 同事实测务必带 cookies.txt (storage_state 导出).
      - playlist URL 走 super().parse() 自动产出多条 ResourceItem.
    """

    def parse(self, url: str) -> List[ResourceItem]:
        """解析 YouTube URL → 1..N 条 ResourceItem (playlist 时 N>1).

        流程:
          1. 调 super().parse() 走 yt-dlp, 拿 base items.
          2. 对 platform='youtube' / 'yt' 的 item 增补 YT 特定 meta.
        """
        items = super().parse(url)
        for item in items:
            if item.platform in ("youtube", "yt"):
                item.meta.setdefault("is_youtube", True)
                # 未来: chapters / thumbnails / subtitles 等可在此添加
        return items


__all__ = ["YouTubeResolver"]
