"""test_xiaohongshu_resolver — XiaohongshuResolver F0.1 智能 dispatch 测试.

Phase 7+ (2026-09-04) 修复:
  - xhs #1 (6a727c8d) 是图集, 之前被 VideoResolver 吞错返 [], 错误标 skipped
  - xhs #2 (6a769354) 是视频, 之前能正常 parse
  - 改后: XiaohongshuResolver 调 yt-dlp 拿 note_info, 判 video / image, dispatch
  - 图片解析下载由 image_fetcher 负责 (用户约束)

测试用 monkeypatch 替换 _fetch_xhs_note_info (避免真 xhs 网络), 验证 dispatch 逻辑.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from multimedia_parsing.resource_fetcher.base import (
    FetchDestination,
    FetchResult,
    ResourceItem,
)
from multimedia_parsing.resource_fetcher.resolvers.xiaohongshu import (
    XiaohongshuResolver,
    _fetch_xhs_note_info,
)


# ---------------------------------------------------------------------------
# F0.1 dispatch 测试 (mock _fetch_xhs_note_info, 不打真 xhs 网络)
# ---------------------------------------------------------------------------

class _FakeVideoItem:
    """fake video_fetcher.ResolvedVideo for VideoResolver monkeypatch."""
    def __init__(self, platform, video_id, title, duration=None, extractor="BiliBili"):
        self.platform = platform
        self.video_id = video_id
        self.title = title
        self.duration = duration
        self.extractor = extractor


def test_parse_image_only_note_dispatches_to_image_resolver():
    """xhs #1 (6a727c8d) — note 只有 imageList → 调 ImageResolver.parse, 产 N 个 image items."""
    # mock note_info: 只有 imageList (无 video.media.stream)
    fake_note_info = {
        "imageList": [
            {"urlDefault": "https://sns-img.xhscdn.com/img1.jpg"},
            {"urlDefault": "https://sns-img.xhscdn.com/img2.jpg"},
            {"urlDefault": "https://sns-img.xhscdn.com/img3.jpg"},
        ],
    }

    with patch(
        "multimedia_parsing.resource_fetcher.resolvers.xiaohongshu._fetch_xhs_note_info",
        return_value=fake_note_info,
    ):
        # mock ImageResolver.parse 返 3 个 image items
        fake_image_items = [
            ResourceItem(
                item_id=f"xhs_img_{i}",
                resource_type="image",
                platform="xiaohongshu",
                source_url="https://www.xiaohongshu.com/explore/test",
                meta={"image_url": f"https://xhs.com/img{i}.jpg"},
            )
            for i in range(3)
        ]
        with patch.object(
            XiaohongshuResolver,
            "_image_resolver",
            new_callable=_make_mock_image_resolver,
        ) if False else patch(
            "multimedia_parsing.resource_fetcher.resolvers.image.ImageResolver.parse",
            return_value=fake_image_items,
        ):
            r = XiaohongshuResolver()
            items = r.parse("https://www.xiaohongshu.com/explore/test")
    # F0.1 终判: 3 image items, resource_type=image
    assert len(items) == 3
    assert all(it.resource_type == "image" for it in items)
    assert [it.item_id for it in items] == ["xhs_img_0", "xhs_img_1", "xhs_img_2"]


def test_parse_video_only_note_dispatches_to_video_resolver():
    """xhs #2 (6a769354) — note 只有 video.media.stream → 调 VideoResolver.parse, 产 1 个 video item."""
    fake_note_info = {
        "video": {
            "media": {
                "stream": {
                    "h264": {"master_url": "https://sns-video.xhscdn.com/stream.m3u8"},
                }
            }
        }
    }

    fake_video_items = [
        ResourceItem(
            item_id="xhs_video_001",
            resource_type="video",
            platform="xiaohongshu",
            source_url="https://www.xiaohongshu.com/explore/test",
            title="一代版本一代神",
        )
    ]
    with patch(
        "multimedia_parsing.resource_fetcher.resolvers.xiaohongshu._fetch_xhs_note_info",
        return_value=fake_note_info,
    ):
        with patch(
            "multimedia_parsing.resource_fetcher.resolvers.video.VideoResolver.parse",
            return_value=fake_video_items,
        ):
            r = XiaohongshuResolver()
            items = r.parse("https://www.xiaohongshu.com/explore/test")
    assert len(items) == 1
    assert items[0].resource_type == "video"
    assert items[0].title == "一代版本一代神"


def test_parse_mixed_video_and_image_prefers_video():
    """note 既有 video stream 又有 imageList → 优先 video (罕见 case, xhs 视频 + 封面图)."""
    fake_note_info = {
        "video": {"media": {"stream": {"h264": {"master_url": "x"}}}},
        "imageList": [{"urlDefault": "https://cover.jpg"}],
    }
    fake_items = [
        ResourceItem(item_id="v1", resource_type="video", platform="xiaohongshu",
                     source_url="u", title="video+cover")
    ]
    with patch(
        "multimedia_parsing.resource_fetcher.resolvers.xiaohongshu._fetch_xhs_note_info",
        return_value=fake_note_info,
    ):
        with patch(
            "multimedia_parsing.resource_fetcher.resolvers.video.VideoResolver.parse",
            return_value=fake_items,
        ) as mock_video:
            with patch(
                "multimedia_parsing.resource_fetcher.resolvers.image.ImageResolver.parse",
            ) as mock_image:
                r = XiaohongshuResolver()
                items = r.parse("https://xhs.com/test")
    assert len(items) == 1
    assert items[0].resource_type == "video"
    mock_video.assert_called_once()
    mock_image.assert_not_called()


def test_parse_note_info_unavailable_falls_back_to_image_resolver():
    """note_info 拿不到 (token 失效 / 网络) → 兜底 ImageResolver (gallery-dl 也能直接处理 xhs URL)."""
    with patch(
        "multimedia_parsing.resource_fetcher.resolvers.xiaohongshu._fetch_xhs_note_info",
        return_value=None,
    ):
        fake_items = [
            ResourceItem(item_id="fb1", resource_type="image", platform="xiaohongshu",
                         source_url="u", meta={"image_url": "x"})
        ]
        with patch(
            "multimedia_parsing.resource_fetcher.resolvers.image.ImageResolver.parse",
            return_value=fake_items,
        ) as mock_image:
            r = XiaohongshuResolver()
            items = r.parse("https://www.xiaohongshu.com/explore/test")
    # 兜底走 image resolver
    assert len(items) == 1
    assert items[0].resource_type == "image"
    mock_image.assert_called_once()


def test_parse_empty_note_falls_back_to_image_resolver():
    """note 存在但既无 video stream 也无 imageList → 兜底 image."""
    fake_note_info = {"type": "video", "title": "Empty note"}  # 无 video.media.stream 也无 imageList
    with patch(
        "multimedia_parsing.resource_fetcher.resolvers.xiaohongshu._fetch_xhs_note_info",
        return_value=fake_note_info,
    ):
        with patch(
            "multimedia_parsing.resource_fetcher.resolvers.image.ImageResolver.parse",
            return_value=[],
        ) as mock_image:
            r = XiaohongshuResolver()
            items = r.parse("https://xhs.com/test")
    assert items == []
    mock_image.assert_called_once()


# ---------------------------------------------------------------------------
# fetch dispatch (F0.1 终判 — item.resource_type 决定)
# ---------------------------------------------------------------------------

def test_fetch_video_item_dispatches_to_video_resolver():
    fake_video_result = FetchResult(
        item_id="v1", resource_type="video", platform="xiaohongshu",
        local_path=Path("/tmp/v.mp4"),
    )
    r = XiaohongshuResolver()
    with patch.object(
        r._video_resolver, "fetch", return_value=fake_video_result
    ) as mock_v:
        with patch.object(r._image_resolver, "fetch") as mock_i:
            item = ResourceItem(item_id="v1", resource_type="video",
                                platform="xiaohongshu", source_url="u")
            dest = FetchDestination(mode="local", download_dir=Path("/tmp"))
            result = r.fetch(item, dest)
    assert result.is_success
    assert result.local_path == Path("/tmp/v.mp4")
    mock_v.assert_called_once()
    mock_i.assert_not_called()


def test_fetch_image_item_dispatches_to_image_resolver():
    fake_image_result = FetchResult(
        item_id="i1", resource_type="image", platform="xiaohongshu",
        local_path=Path("/tmp/i.jpg"),
    )
    r = XiaohongshuResolver()
    with patch.object(
        r._image_resolver, "fetch", return_value=fake_image_result
    ) as mock_i:
        with patch.object(r._video_resolver, "fetch") as mock_v:
            item = ResourceItem(item_id="i1", resource_type="image",
                                platform="xiaohongshu", source_url="u")
            dest = FetchDestination(mode="local", download_dir=Path("/tmp"))
            result = r.fetch(item, dest)
    assert result.is_success
    mock_i.assert_called_once()
    mock_v.assert_not_called()


def test_fetch_unsupported_resource_type_returns_error():
    """D2 预留的 audio / model — 现在不支持, 返 error (跟 VideoResolver.fetch 同模式)."""
    r = XiaohongshuResolver()
    item = ResourceItem(item_id="a1", resource_type="audio",
                        platform="xiaohongshu", source_url="u")
    dest = FetchDestination(mode="local", download_dir=Path("/tmp"))
    result = r.fetch(item, dest)
    assert not result.is_success
    assert "XiaohongshuResolver cannot fetch resource_type='audio'" in (result.error or "")


# ---------------------------------------------------------------------------
# router.py 注册 (xhs 走 XiaohongshuResolver)
# ---------------------------------------------------------------------------

def test_router_xiaohongshu_uses_mixed_resolver():
    """router.resolve(xhs URL) → XiaohongshuResolver (不是 VideoResolver)."""
    from multimedia_parsing.resource_fetcher.router import resolve
    from multimedia_parsing.resource_fetcher.resolvers.xiaohongshu import XiaohongshuResolver

    assert resolve("https://www.xiaohongshu.com/explore/abc") is XiaohongshuResolver
    assert resolve("https://www.xiaohongshu.com/discovery/item/xyz") is XiaohongshuResolver
    assert resolve("https://xhslink.com/a/abc") is XiaohongshuResolver


# ---------------------------------------------------------------------------
# placeholder (avoid lint warning "imported but unused" if _make_mock_image_resolver False branch)
# ---------------------------------------------------------------------------
def _make_mock_image_resolver():
    return None
