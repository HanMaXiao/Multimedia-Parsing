"""test_resource_fetcher_base — dataclass 不变式 + Protocol runtime check.

权威定义: docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md §4 + §5.2 + §9.
phase: 2026-09-03-universal-resource-fetch Phase 1 Commit 1.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from multimedia_parsing.resource_fetcher.base import (
    VALID_MODES,
    FetchDestination,
    FetchResult,
    ResourceFetchError,
    ResourceItem,
    ResourceResolver,
    ResourceType,
)
from multimedia_parsing.resource_fetcher.resolvers.image import ImageResolver
from multimedia_parsing.resource_fetcher.resolvers.video import VideoResolver


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _oss_config() -> "OssConfig":  # type: ignore[name-defined]
    from multimedia_parsing.video_fetcher.manifest import OssConfig

    return OssConfig(
        provider="aliyun_oss",
        endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        region="cn-hangzhou",
        bucket="b",
        access_key_id="k",
        secret_access_key="s",
    )


# ---------------------------------------------------------------------------
# ResourceType enum
# ---------------------------------------------------------------------------


def test_resource_type_values():
    assert ResourceType.VIDEO.value == "video"
    assert ResourceType.IMAGE.value == "image"
    assert ResourceType.AUDIO.value == "audio"  # D2 预留
    assert ResourceType.MODEL.value == "model"  # D2 预留


def test_resource_type_is_str_subclass():
    """ResourceType 继承 str — 可直接 JSON 序列化 / 字典 key."""
    assert ResourceType.VIDEO == "video"  # type: ignore[comparison-overlap]
    assert ResourceType("video") is ResourceType.VIDEO


# ---------------------------------------------------------------------------
# ResourceItem
# ---------------------------------------------------------------------------


def test_resource_item_minimal_fields_only():
    item = ResourceItem(
        item_id="bilibili_BV1xx",
        resource_type="video",
        platform="bilibili",
        source_url="https://www.bilibili.com/video/BV1xx",
    )
    assert item.title == ""
    assert item.thumbnail == ""
    assert item.meta == {}


def test_resource_item_full_fields():
    item = ResourceItem(
        item_id="bilibili_BV1xx",
        resource_type="video",
        platform="bilibili",
        source_url="https://www.bilibili.com/video/BV1xx",
        title="foo",
        thumbnail="https://example.com/thumb.jpg",
        meta={"duration": 60.0, "extractor": "BiliBili"},
    )
    assert item.title == "foo"
    assert item.thumbnail == "https://example.com/thumb.jpg"
    assert item.meta["duration"] == 60.0


def test_resource_item_frozen():
    item = ResourceItem(item_id="x", resource_type="video", platform="y", source_url="z")
    with pytest.raises((AttributeError, Exception)):
        item.title = "changed"  # type: ignore[misc]


def test_resource_item_default_meta_is_independent_per_instance():
    """frozen dataclass 的 default_factory 每次返新 dict — 跨实例不共享."""
    a = ResourceItem(item_id="a", resource_type="video", platform="p", source_url="s")
    b = ResourceItem(item_id="b", resource_type="video", platform="p", source_url="s")
    a.meta["k"] = 1
    assert "k" not in b.meta


# ---------------------------------------------------------------------------
# FetchDestination — mode 互斥校验
# ---------------------------------------------------------------------------


def test_fetch_destination_local_requires_download_dir():
    with pytest.raises(ValueError, match="download_dir is required"):
        FetchDestination(mode="local", download_dir=None)


def test_fetch_destination_local_rejects_oss():
    with pytest.raises(ValueError, match="oss must be null"):
        FetchDestination(mode="local", download_dir=Path("/tmp"), oss=_oss_config())


def test_fetch_destination_oss_requires_oss():
    with pytest.raises(ValueError, match="oss is required"):
        FetchDestination(mode="oss", download_dir=None)


def test_fetch_destination_oss_rejects_download_dir():
    with pytest.raises(ValueError, match="download_dir must be null"):
        FetchDestination(mode="oss", download_dir=Path("/tmp"), oss=_oss_config())


def test_fetch_destination_both_requires_download_dir():
    with pytest.raises(ValueError, match="download_dir is required"):
        FetchDestination(mode="both", oss=_oss_config())


def test_fetch_destination_both_requires_oss():
    with pytest.raises(ValueError, match="oss is required"):
        FetchDestination(mode="both", download_dir=Path("/tmp"))


def test_fetch_destination_invalid_mode():
    with pytest.raises(ValueError, match="mode must be one of"):
        FetchDestination(mode="ftp", download_dir=Path("/tmp"))


def test_fetch_destination_valid_local():
    d = FetchDestination(mode="local", download_dir=Path("/tmp/dl"))
    assert d.mode == "local"
    assert d.download_dir == Path("/tmp/dl")
    assert d.oss is None


def test_fetch_destination_valid_oss():
    oss = _oss_config()
    d = FetchDestination(mode="oss", oss=oss)
    assert d.mode == "oss"
    assert d.oss is oss
    assert d.download_dir is None


def test_fetch_destination_valid_both():
    """spec §5.2 D3: 'both' 模式是新增合法 mode — 本地 + OSS 都做."""
    d = FetchDestination(mode="both", download_dir=Path("/tmp/dl"), oss=_oss_config())
    assert d.mode == "both"
    assert d.download_dir == Path("/tmp/dl")
    assert d.oss is not None


# ---------------------------------------------------------------------------
# FetchResult — spec §9 成功/失败/both 部分成功
# ---------------------------------------------------------------------------


def test_fetch_result_success_local_only():
    r = FetchResult(
        item_id="x",
        resource_type="video",
        platform="bilibili",
        local_path=Path("/tmp/v.mp4"),
    )
    assert r.is_success
    assert r.oss_url is None
    assert r.oss_error is None
    assert r.error is None


def test_fetch_result_success_oss_only():
    r = FetchResult(
        item_id="x",
        resource_type="image",
        platform="weibo",
        oss_url="https://oss.example.com/x.jpg",
    )
    assert r.is_success


def test_fetch_result_both_partial_oss_fail_is_still_success():
    """spec §9: both 模式 OSS 失败但本地已成功 → success + oss_error 记录."""
    r = FetchResult(
        item_id="x",
        resource_type="image",
        platform="weibo",
        local_path=Path("/tmp/x.jpg"),
        oss_url=None,
        oss_error="OSS upload failed: 403",
    )
    assert r.is_success, "本地有 = 整体 success, oss 失败单独字段记录"
    assert r.oss_error is not None


def test_fetch_result_failed_with_error():
    r = FetchResult(
        item_id="x",
        resource_type="video",
        platform="bilibili",
        error="network timeout",
    )
    assert not r.is_success


def test_fetch_result_empty_is_not_success():
    """既没 local_path 也没 oss_url → 不算成功."""
    r = FetchResult(item_id="x", resource_type="video", platform="y")
    assert not r.is_success


# ---------------------------------------------------------------------------
# ResourceResolver Protocol
# ---------------------------------------------------------------------------


def test_resolver_protocol_runtime_check_video():
    v = VideoResolver()
    assert isinstance(v, ResourceResolver)


def test_resolver_protocol_runtime_check_image():
    i = ImageResolver()
    assert isinstance(i, ResourceResolver)


def test_resolver_protocol_rejects_non_resolver():
    """非协议实现(无 parse / fetch 方法)应被 isinstance 拒.

    Note: Python ``runtime_checkable`` Protocol 只检查方法名存在, 不检查签名.
    真正想验证签名一致性得用 mypy/pyright, 运行时 isinstance 无法做到.
    """

    class NotAResolver:
        pass  # 没有 parse / fetch 方法

    class PartialResolver:
        # 只有 parse 没有 fetch — 不应满足协议
        def parse(self, url):
            return []

    assert not isinstance(NotAResolver(), ResourceResolver)
    assert not isinstance(PartialResolver(), ResourceResolver)


# ---------------------------------------------------------------------------
# ResourceFetchError
# ---------------------------------------------------------------------------


def test_resource_fetch_error_is_runtime_error():
    """ResourceFetchError 继承 RuntimeError — 现有 except RuntimeError 也能 catch."""
    assert issubclass(ResourceFetchError, RuntimeError)


# ---------------------------------------------------------------------------
# VALID_MODES 白名单
# ---------------------------------------------------------------------------


def test_valid_modes_three():
    assert VALID_MODES == frozenset({"local", "oss", "both"})


# Note: Resolver 真行为测试在 test_resource_fetcher_resolvers.py.
# Commit 1 的 stub tests (parse 返空 / fetch 标 "not yet implemented") 已被 Commit 2 真包装取代.

