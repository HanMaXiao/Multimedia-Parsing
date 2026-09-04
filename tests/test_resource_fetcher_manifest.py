"""test_resource_fetcher_manifest — unified manifest 校验.

权威定义: docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md §5.2.
phase: 2026-09-03-universal-resource-fetch Phase 1 Commit 2.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from multimedia_parsing.resource_fetcher.manifest import (
    ManifestValidationError,
    ResourceFetchItem,
    ResourceFetchManifest,
    ResourceParseManifest,
    load_resource_fetch_manifest,
    load_resource_parse_manifest,
    parse_resource_fetch_manifest_dict,
    parse_resource_parse_manifest_dict,
)
from multimedia_parsing.video_fetcher.manifest import ManifestValidationError as VfMVE  # noqa: F401


# ---------------------------------------------------------------------------
# ResourceParseManifest
# ---------------------------------------------------------------------------


def test_parse_manifest_minimal():
    raw = {"batch_id": "b1", "urls": ["https://www.bilibili.com/video/BV1xx"]}
    m = parse_resource_parse_manifest_dict(raw)
    assert m.batch_id == "b1"
    assert m.urls == ["https://www.bilibili.com/video/BV1xx"]


def test_parse_manifest_multiple_urls():
    raw = {
        "batch_id": "b1",
        "urls": [
            "https://www.bilibili.com/video/BV1xx",
            "https://weibo.com/123",
            "https://example.com/foo",
        ],
    }
    m = parse_resource_parse_manifest_dict(raw)
    assert len(m.urls) == 3


@pytest.mark.parametrize(
    "raw, err_match",
    [
        ("not a dict", "manifest must be a JSON object"),
        ([1, 2, 3], "manifest must be a JSON object"),
        ({}, "batch_id is required"),  # 空 dict 是 dict, 只是缺字段
        ({"urls": ["x"]}, "batch_id is required"),
        ({"batch_id": ""}, "batch_id must be a non-empty string"),
        ({"batch_id": "b1"}, "urls is required"),
        ({"batch_id": "b1", "urls": []}, "urls must be a non-empty"),
        ({"batch_id": "b1", "urls": "not a list"}, "urls must be a JSON array"),
        ({"batch_id": "b1", "urls": [123]}, "must be a string"),
        ({"batch_id": "b1", "urls": [""]}, "must be non-empty"),
        ({"batch_id": "b1", "urls": ["ftp://example.com"]}, "must start with http"),
        ({"batch_id": "b1", "urls": ["javascript:alert(1)"]}, "must start with http"),
    ],
)
def test_parse_manifest_invalid(raw, err_match):
    with pytest.raises(ManifestValidationError, match=err_match):
        parse_resource_parse_manifest_dict(raw)


def test_load_parse_manifest_from_file(tmp_path):
    p = tmp_path / "parse_manifest.json"
    p.write_text(
        json.dumps({"batch_id": "b1", "urls": ["https://bilibili.com/video/BV1"]}),
        encoding="utf-8",
    )
    m = load_resource_parse_manifest(p)
    assert m.batch_id == "b1"


def test_load_parse_manifest_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_resource_parse_manifest(tmp_path / "nope.json")


# ---------------------------------------------------------------------------
# ResourceFetchManifest — 字段 + 校验
# ---------------------------------------------------------------------------


def _oss_config_dict() -> dict:
    return {
        "provider": "aliyun_oss",
        "endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
        "region": "cn-hangzhou",
        "bucket": "b",
        "access_key_id": "k",
        "secret_access_key": "s",
    }


def _video_item_dict() -> dict:
    return {
        "url": "https://www.bilibili.com/video/BV1xx",
        "source_url": "https://www.bilibili.com/video/BV1xx",
        "resource_type": "video",
        "item_id": "bilibili_BV1xx",
        "platform": "bilibili",
        "title": "foo",
    }


def _image_item_dict() -> dict:
    return {
        "url": "https://weibo.com/img.jpg",
        "source_url": "https://weibo.com/123",
        "resource_type": "image",
        "item_id": "img_abc",
        "platform": "weibo",
        "title": "img",
        "meta": {"image_url": "https://weibo.com/img.jpg", "width": 800, "height": 600},
    }


def test_fetch_manifest_mode_local():
    raw = {
        "batch_id": "b1",
        "mode": "local",
        "download_dir": "/tmp/dl",
        "items": [_video_item_dict()],
    }
    m = parse_resource_fetch_manifest_dict(raw)
    assert m.batch_id == "b1"
    assert m.mode == "local"
    assert m.download_dir == Path("/tmp/dl")
    assert m.oss is None
    assert len(m.items) == 1
    assert isinstance(m.items[0], ResourceFetchItem)
    assert m.items[0].item_id == "bilibili_BV1xx"


def test_fetch_manifest_mode_oss():
    raw = {
        "batch_id": "b1",
        "mode": "oss",
        "oss": _oss_config_dict(),
        "items": [_image_item_dict()],
    }
    m = parse_resource_fetch_manifest_dict(raw)
    assert m.mode == "oss"
    assert m.oss is not None
    assert m.oss.provider == "aliyun_oss"
    assert m.download_dir is None
    assert m.items[0].meta["image_url"] == "https://weibo.com/img.jpg"


def test_fetch_manifest_mode_both():
    """spec §5.2 D3: 'both' 是新增合法 mode (本地 + OSS 都做)."""
    raw = {
        "batch_id": "b1",
        "mode": "both",
        "download_dir": "/tmp/dl",
        "oss": _oss_config_dict(),
        "items": [_image_item_dict()],
    }
    m = parse_resource_fetch_manifest_dict(raw)
    assert m.mode == "both"
    assert m.download_dir == Path("/tmp/dl")
    assert m.oss is not None


def test_fetch_manifest_optional_cookie_file_and_account_id():
    """spec §5.2 + 现有 video_fetcher 模式: cookie_file / account_id 可选 batch-level."""
    raw = {
        "batch_id": "b1",
        "mode": "local",
        "download_dir": "/tmp/dl",
        "cookie_file": "/tmp/cookies.txt",
        "account_id": "user-1",
        "items": [_video_item_dict()],
    }
    m = parse_resource_fetch_manifest_dict(raw)
    assert m.cookie_file == Path("/tmp/cookies.txt")
    assert m.account_id == "user-1"


# ---------------------------------------------------------------------------
# mode 互斥校验
# ---------------------------------------------------------------------------


def test_fetch_manifest_local_with_oss_is_error():
    raw = {
        "batch_id": "b1",
        "mode": "local",
        "download_dir": "/tmp/dl",
        "oss": _oss_config_dict(),
        "items": [_video_item_dict()],
    }
    with pytest.raises(ManifestValidationError, match="oss must be null"):
        parse_resource_fetch_manifest_dict(raw)


def test_fetch_manifest_oss_with_download_dir_is_error():
    raw = {
        "batch_id": "b1",
        "mode": "oss",
        "oss": _oss_config_dict(),
        "download_dir": "/tmp/dl",
        "items": [_video_item_dict()],
    }
    with pytest.raises(ManifestValidationError, match="download_dir must be null"):
        parse_resource_fetch_manifest_dict(raw)


def test_fetch_manifest_local_missing_download_dir_is_error():
    raw = {
        "batch_id": "b1",
        "mode": "local",
        "items": [_video_item_dict()],
    }
    with pytest.raises(ManifestValidationError, match="download_dir is required"):
        parse_resource_fetch_manifest_dict(raw)


def test_fetch_manifest_oss_missing_oss_is_error():
    raw = {
        "batch_id": "b1",
        "mode": "oss",
        "items": [_video_item_dict()],
    }
    with pytest.raises(ManifestValidationError, match="oss is required"):
        parse_resource_fetch_manifest_dict(raw)


def test_fetch_manifest_both_missing_download_dir_is_error():
    raw = {
        "batch_id": "b1",
        "mode": "both",
        "oss": _oss_config_dict(),
        "items": [_video_item_dict()],
    }
    with pytest.raises(ManifestValidationError, match="download_dir is required"):
        parse_resource_fetch_manifest_dict(raw)


def test_fetch_manifest_both_missing_oss_is_error():
    raw = {
        "batch_id": "b1",
        "mode": "both",
        "download_dir": "/tmp/dl",
        "items": [_video_item_dict()],
    }
    with pytest.raises(ManifestValidationError, match="oss is required"):
        parse_resource_fetch_manifest_dict(raw)


def test_fetch_manifest_invalid_mode():
    raw = {
        "batch_id": "b1",
        "mode": "ftp",
        "download_dir": "/tmp/dl",
        "items": [_video_item_dict()],
    }
    with pytest.raises(ManifestValidationError, match="mode must be one of"):
        parse_resource_fetch_manifest_dict(raw)


# ---------------------------------------------------------------------------
# items 字段校验
# ---------------------------------------------------------------------------


def test_fetch_manifest_items_missing():
    raw = {
        "batch_id": "b1",
        "mode": "local",
        "download_dir": "/tmp/dl",
    }
    with pytest.raises(ManifestValidationError, match="items is required"):
        parse_resource_fetch_manifest_dict(raw)


def test_fetch_manifest_items_empty():
    raw = {
        "batch_id": "b1",
        "mode": "local",
        "download_dir": "/tmp/dl",
        "items": [],
    }
    with pytest.raises(ManifestValidationError, match="items must be a non-empty"):
        parse_resource_fetch_manifest_dict(raw)


@pytest.mark.parametrize(
    "missing_field",
    ["url", "source_url", "resource_type", "item_id", "platform"],
)
def test_fetch_manifest_item_required_fields(missing_field):
    item = _video_item_dict()
    del item[missing_field]
    raw = {
        "batch_id": "b1",
        "mode": "local",
        "download_dir": "/tmp/dl",
        "items": [item],
    }
    with pytest.raises(ManifestValidationError, match=missing_field):
        parse_resource_fetch_manifest_dict(raw)


def test_fetch_manifest_item_url_must_be_http():
    item = _video_item_dict()
    item["url"] = "ftp://example.com"
    raw = {
        "batch_id": "b1",
        "mode": "local",
        "download_dir": "/tmp/dl",
        "items": [item],
    }
    with pytest.raises(ManifestValidationError, match="must start with http"):
        parse_resource_fetch_manifest_dict(raw)


def test_fetch_manifest_item_resource_type_must_be_valid():
    item = _video_item_dict()
    item["resource_type"] = "audio_unknown"
    raw = {
        "batch_id": "b1",
        "mode": "local",
        "download_dir": "/tmp/dl",
        "items": [item],
    }
    with pytest.raises(ManifestValidationError, match="resource_type must be one of"):
        parse_resource_fetch_manifest_dict(raw)


def test_fetch_manifest_item_meta_default_empty():
    item = _video_item_dict()
    raw = {
        "batch_id": "b1",
        "mode": "local",
        "download_dir": "/tmp/dl",
        "items": [item],
    }
    m = parse_resource_fetch_manifest_dict(raw)
    assert m.items[0].meta == {}


def test_fetch_manifest_item_meta_passed_through():
    item = _image_item_dict()
    raw = {
        "batch_id": "b1",
        "mode": "local",
        "download_dir": "/tmp/dl",
        "items": [item],
    }
    m = parse_resource_fetch_manifest_dict(raw)
    assert m.items[0].meta["image_url"] == "https://weibo.com/img.jpg"


def test_fetch_manifest_mixed_video_and_image_items():
    """F0.1 终判: items 列表里 video + image 混发, 各自 resource_type 正确解析."""
    raw = {
        "batch_id": "b1",
        "mode": "local",
        "download_dir": "/tmp/dl",
        "items": [_video_item_dict(), _image_item_dict()],
    }
    m = parse_resource_fetch_manifest_dict(raw)
    assert m.items[0].resource_type == "video"
    assert m.items[1].resource_type == "image"


# ---------------------------------------------------------------------------
# 跨包异常类统一 (F0.5 后续 Commit 3 OSS 上提后仍兼容)
# ---------------------------------------------------------------------------


def test_manifest_validation_error_is_video_fetcher_alias():
    """spec F2.1 跨包复用: 同一个异常类. Commit 3 移 OssUploader 后异常类来源应统一."""
    from multimedia_parsing.resource_fetcher.manifest import ManifestValidationError as RfMVE

    # 两个 import 路径都应指向同一个 class (F0.5 Commit 3 落地后, 这条断言会仍然通过)
    assert RfMVE is VfMVE


# ---------------------------------------------------------------------------
# 边界 — load from file
# ---------------------------------------------------------------------------


def test_load_fetch_manifest_from_file(tmp_path):
    p = tmp_path / "fetch_manifest.json"
    p.write_text(
        json.dumps(
            {
                "batch_id": "b1",
                "mode": "local",
                "download_dir": "/tmp/dl",
                "items": [_video_item_dict()],
            }
        ),
        encoding="utf-8",
    )
    m = load_resource_fetch_manifest(p)
    assert m.batch_id == "b1"
    assert m.items[0].item_id == "bilibili_BV1xx"


def test_load_fetch_manifest_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_resource_fetch_manifest(tmp_path / "nope.json")

