"""resolvers.xiaohongshu — xhs 智能 dispatch resolver (F0.1 终判).

xhs explore URL 实际可能是视频 (note.video.media.stream) 或图集 (note.imageList)。
yt-dlp xiaohongshu extractor 默认按 video 找 formats,图集无 video formats → 抛
DownloadError "No video formats found!"。VideoResolver.parse 吞掉所有异常返 [],
完全忽略图集。

修法 (用户选 A, 2026-09-04):
  调 yt-dlp 拿 note_info (直接调 _extract_note_info 拿原始 note dict, 跳过
  formats check) → F0.1 终判:
    - note.video.media.stream 存在 → 调 VideoResolver.parse (走 video_fetcher)
    - note.imageList 非空 → 调 ImageResolver.parse (走 image_fetcher / gallery-dl)
  图片解析下载**由 image_fetcher 负责**, 不复用 yt-dlp (用户约束:
  "图片解析下载也不应该由 yt-dlp 负责").

router 注册: xhs.com / xhslink.com → XiaohongshuResolver (替换原 VideoResolver 注册).
"""
from __future__ import annotations

import logging
from typing import List, Optional

from ..base import ResourceItem, ResourceResolver
from .image import ImageResolver
from .video import VideoResolver

log = logging.getLogger(__name__)


def _traverse(obj, *path):
    """安全 traverse nested dict, 任意 path 缺失返 None."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _fetch_xhs_note_info(url: str, cookie_file: Optional[str]) -> Optional[dict]:
    """调 yt-dlp 拿 xhs note_info (不调 process, 跳过 format check).

    用 yt-dlp 内部 API (`_search_json` + extractor instance) 直接拿到
    initial_state.note.noteDetailMap.<note_id>.note dict (包含 imageList +
    video.media.stream). 即便图集没有 video formats 也不抛 error — 我们只
    需要原始 note data 来判 type + 拿图.

    Returns:
        dict: note_info 来自 initial_state, 包含 imageList / video 字段.
        None: 解析失败 (网络 / xhs 反爬 / token 失效 / 帖子不存在).
    """
    try:
        import yt_dlp
    except ImportError:
        return None

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    if cookie_file:
        opts["cookiefile"] = str(cookie_file)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            # 拿 xiaohongshu extractor instance
            ie = next(
                (c(ydl) for c in ydl._ies if "xiaohongshu" in c.IE_NAME.lower()),
                None,
            )
            if ie is None:
                log.warning("xiaohongshu extractor not found in yt-dlp")
                return None
            # extract initial_state 走 _search_json 走 (yhs.com 的 embedded JSON)
            initial_state = ie._search_json(url, None, None) if hasattr(ie, "_search_json") else None
            if not initial_state:
                # fallback: 调 _real_extract 但用 _perform_login / _download_initial_data 拿
                # 这条路径会 throw No formats, 我们 catch + 只拿 note dict
                try:
                    # 直接调 _real_extract 走一部分: 它会失败在 formats, 但
                    # 之前的 _download_initial_data 已经被调, 我们能拿网页 JSON
                    # 用 _extract_note_data_from_initial_state helper:
                    webpage = ie._download_webpage(url, None)
                    initial_state = ie._search_json(webpage, None, "window.__INITIAL_STATE__=")
                except Exception as exc:
                    log.debug("xhs webpage fetch failed: %s", exc)
            if not initial_state:
                return None
            # 从 initial_state 拿 note dict (跟 extractor 内部一样)
            note_id = _traverse(initial_state, ("note", "noteDetailMap")) or {}
            # note_id 是 dict 的 keys 取第一个 (单帖子)
            if not note_id:
                return None
            first_id = next(iter(note_id.keys()), None)
            if not first_id:
                return None
            note_info = _traverse(note_id, (first_id, "note"))
            return note_info
    except Exception as exc:
        log.debug("xhs note_info fetch failed: %s", exc)
        return None


class XiaohongshuResolver:
    """xhs 智能 dispatch — F0.1 终判按 note 内容决定走 video 还是 image resolver.

    Parameters
    ----------
    cookie_file:
        Netscape cookies.txt 路径 (xhs 反爬严格, 通常必需).
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
        # 构造时传 None cookie, parse 阶段不需要
        self._video_resolver = VideoResolver(
            cookie_file=cookie_file, account_id=account_id
        )
        self._image_resolver = ImageResolver()

    # 满足 Protocol (runtime_checkable)
    def parse(self, url: str) -> List[ResourceItem]:
        """F0.1 终判: 调 yt-dlp 拿 note_info, 判 video / image, dispatch.

        异常 / note_info 拿不到 → 兜底 image resolver (gallery-dl 也能
        处理 xhs URL, 跟 VideoResolver 默认走 video resolver 不同).
        """
        note_info = _fetch_xhs_note_info(url, self.cookie_file)

        has_video_stream = bool(
            _traverse(note_info, "video", "media", "stream")
        )
        has_image_list = bool(
            _traverse(note_info, "imageList")
        )

        # F0.1 终判逻辑
        if has_video_stream and not has_image_list:
            return self._video_resolver.parse(url)
        if has_image_list and not has_video_stream:
            return self._image_resolver.parse(url)
        if has_video_stream and has_image_list:
            # 两者都有 (xhs 视频 + 字幕图 / 封面图) → 优先 video
            return self._video_resolver.parse(url)
        # 都没有 (note_info 拿不到 / token 失效) → 兜底 image
        return self._image_resolver.parse(url)

    def fetch(self, item: ResourceItem, dest) -> "object":  # type: ignore[no-untyped-def]
        """dispatch fetch 到对应 resolver — F0.1 终判, ResourceItem.resource_type 决定."""
        if item.resource_type == "video":
            return self._video_resolver.fetch(item, dest)
        if item.resource_type == "image":
            return self._image_resolver.fetch(item, dest)
        # audio / model 等 D2 预留 — 跟 VideoResolver.fetch 同样的 error 模式
        from ..base import FetchResult
        return FetchResult(
            item_id=item.item_id,
            resource_type=item.resource_type,
            platform=item.platform,
            error=f"XiaohongshuResolver cannot fetch resource_type={item.resource_type!r}",
        )


__all__ = ["XiaohongshuResolver"]
