"""test_xiaohongshu_resolver — XiaohongshuResolver F0.1 智能 dispatch 测试.

Phase 7+ (2026-09-04) 设计:
  - 旧实现: 调 yt-dlp 内部 API 拿 note_info, 判 video.stream / imageList, dispatch
  - 新实现 (2026-09-04 修订): video-first fallback — VideoResolver.parse 拿到 video
    formats 就返 video items; 失败 → 兜底 ImageResolver.parse 拿 imageList
  - 图片解析下载由 image_fetcher 负责 (用户约束)
  - 简化原因: 新版 yt-dlp 顶层 extract_info 不暴露 note_info,旧路径不稳

测试用 monkeypatch 替换 VideoResolver.parse / ImageResolver.parse, 验证 dispatch 逻辑,
不打真 xhs 网络.
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
)


# ---------------------------------------------------------------------------
# F0.1 dispatch 测试 (mock video/image resolver, 不打真 xhs 网络)
# ---------------------------------------------------------------------------


def test_parse_video_first_returns_video_items():
    """xhs video URL — VideoResolver.parse 返 1 video item, 不调 image 兜底."""
    fake_video_items = [
        ResourceItem(
            item_id="xhs_video_001",
            resource_type="video",
            platform="xiaohongshu",
            source_url="https://www.xiaohongshu.com/explore/test",
            title="社死现场",
            meta={"video_id": "6a6fa5f4", "duration": 98.3},
        )
    ]
    with patch(
        "multimedia_parsing.resource_fetcher.resolvers.video.VideoResolver.parse",
        return_value=fake_video_items,
    ) as mock_video:
        with patch(
            "multimedia_parsing.resource_fetcher.resolvers.image.ImageResolver.parse",
        ) as mock_image:
            r = XiaohongshuResolver()
            items = r.parse("https://www.xiaohongshu.com/explore/test")
    assert len(items) == 1
    assert items[0].resource_type == "video"
    assert items[0].title == "社死现场"
    mock_video.assert_called_once()
    mock_image.assert_not_called()


def test_parse_video_fails_falls_back_to_image_items():
    """xhs image URL — VideoResolver.parse 返 [] → 兜底 ImageResolver.parse 返 N image items."""
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
    with patch(
        "multimedia_parsing.resource_fetcher.resolvers.video.VideoResolver.parse",
        return_value=[],  # xhs 图集无 video formats → yt-dlp 抛 DownloadError → 吞掉返 []
    ) as mock_video:
        with patch(
            "multimedia_parsing.resource_fetcher.resolvers.image.ImageResolver.parse",
            return_value=fake_image_items,
        ) as mock_image:
            r = XiaohongshuResolver()
            items = r.parse("https://www.xiaohongshu.com/explore/test")
    assert len(items) == 3
    assert all(it.resource_type == "image" for it in items)
    assert [it.item_id for it in items] == ["xhs_img_0", "xhs_img_1", "xhs_img_2"]
    mock_video.assert_called_once()
    mock_image.assert_called_once()


def test_parse_video_raises_falls_back_to_image():
    """video resolver 抛异常 (网络 / 反爬) → 兜底 image (跟 _fetch_xhs_note_info 失败行为同)."""
    fake_image_items = [
        ResourceItem(item_id="fb1", resource_type="image", platform="xiaohongshu",
                     source_url="u", meta={"image_url": "x"})
    ]
    with patch(
        "multimedia_parsing.resource_fetcher.resolvers.video.VideoResolver.parse",
        side_effect=RuntimeError("network error"),
    ) as mock_video:
        with patch(
            "multimedia_parsing.resource_fetcher.resolvers.image.ImageResolver.parse",
            return_value=fake_image_items,
        ) as mock_image:
            r = XiaohongshuResolver()
            items = r.parse("https://www.xiaohongshu.com/explore/test")
    assert len(items) == 1
    assert items[0].resource_type == "image"
    mock_video.assert_called_once()
    mock_image.assert_called_once()


def test_parse_both_resolvers_fail_returns_empty():
    """video + image resolver 都失败 (极端: xhs 完全不可达) → 返 []."""
    with patch(
        "multimedia_parsing.resource_fetcher.resolvers.video.VideoResolver.parse",
        return_value=[],
    ) as mock_video:
        with patch(
            "multimedia_parsing.resource_fetcher.resolvers.image.ImageResolver.parse",
            return_value=[],
        ) as mock_image:
            r = XiaohongshuResolver()
            items = r.parse("https://www.xiaohongshu.com/explore/test")
    assert items == []
    mock_video.assert_called_once()
    mock_image.assert_called_once()


def test_parse_image_raises_returns_empty():
    """video 拿到, image 抛 (不太可能但兜底) → 返 video items."""
    fake_video_items = [
        ResourceItem(item_id="v1", resource_type="video", platform="xiaohongshu",
                     source_url="u", title="mixed case")
    ]
    with patch(
        "multimedia_parsing.resource_fetcher.resolvers.video.VideoResolver.parse",
        return_value=fake_video_items,
    ) as mock_video:
        with patch(
            "multimedia_parsing.resource_fetcher.resolvers.image.ImageResolver.parse",
            side_effect=RuntimeError("should not be called"),
        ) as mock_image:
            r = XiaohongshuResolver()
            items = r.parse("https://xhs.com/test")
    # video 拿到就直接返, 不调 image
    assert len(items) == 1
    assert items[0].resource_type == "video"
    mock_video.assert_called_once()
    mock_image.assert_not_called()


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
