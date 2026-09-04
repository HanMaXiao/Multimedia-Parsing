"""test_image_fetcher_manifest.py — image_fetcher/manifest.py 单元测试。

权威定义:docs/superpowers/specs/2026-09-02-image-fetch-oss-design.md §5。
镜像 test_video_fetcher_manifest.py 的覆盖维度,差异:
  - 两种 manifest:ImageParseManifest(解析阶段)+ ImageFetchManifest(下载/上传阶段)
  - mode 互斥校验(local ↔ oss ↔ 缺省)
  - ImageEntry 嵌套校验(url + source_url + 可选 width/height)
  - OssConfig 跨包复用(从 video_fetcher 导入,不在 image_fetcher 重复定义)

覆盖范围:
  - 合法 manifest 解析成功(parse / fetch + local / fetch + oss)
  - 缺字段 / 类型错 / 边界不通过 → ManifestValidationError
  - ImageEntry:width/height 可选 / 必为正整数
  - mode 互斥校验(local 必带 download_dir,oss 必带 oss 配置)
  - JSON 文件加载(load_manifest)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from multimedia_parsing.image_fetcher.manifest import (
    ImageEntry,
    ImageFetchManifest,
    ImageParseManifest,
    ManifestValidationError,
    load_fetch_manifest,
    load_parse_manifest,
    parse_fetch_manifest_dict,
    parse_parse_manifest_dict,
)
from multimedia_parsing.video_fetcher.manifest import OssConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _valid_oss_dict() -> dict:
    return {
        "provider": "aliyun_oss",
        "endpoint": "oss-cn-hangzhou.aliyuncs.com",
        "region": "cn-hangzhou",
        "bucket": "my-image-bucket",
        "access_key_id": "AKID",
        "secret_access_key": "SK",
        "path_prefix": "images",
        "public_base_url": "https://cdn.example.com",
    }


def _valid_parse_manifest_dict(**overrides) -> dict:
    base = {
        "batch_id": "img-20260902-001",
        "urls": [
            "https://example.com/article/123",
            "https://www.pixiv.net/artworks/98765432",
        ],
    }
    base.update(overrides)
    return base


def _valid_image_entry_dict(**overrides) -> dict:
    base = {
        "url": "https://i.pximg.net/img-original/img/2026/09/02/00/00/00/12345678_p0.jpg",
        "source_url": "https://www.pixiv.net/artworks/98765432",
        "width": 1920,
        "height": 1080,
    }
    base.update(overrides)
    return base


def _valid_fetch_manifest_dict(**overrides) -> dict:
    base = {
        "batch_id": "img-20260902-001",
        "mode": "local",
        "download_dir": "D:/pics/img-20260902-001",
        "images": [
            _valid_image_entry_dict(),
            _valid_image_entry_dict(
                url="https://i.pximg.net/img-original/img/2026/09/02/00/00/00/87654321_p0.jpg",
                source_url="https://www.pixiv.net/artworks/98765433",
                width=None,
                height=None,
            ),
        ],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# ImageParseManifest 正常路径
# ---------------------------------------------------------------------------


def test_parse_parse_manifest_returns_dataclass():
    """合法 ImageParseManifest → 解析成功,字段映射正确。"""
    m = parse_parse_manifest_dict(_valid_parse_manifest_dict())
    assert isinstance(m, ImageParseManifest)
    assert m.batch_id == "img-20260902-001"
    assert len(m.urls) == 2
    assert m.urls[0] == "https://example.com/article/123"


# ---------------------------------------------------------------------------
# ImageParseManifest 边界 / 错误
# ---------------------------------------------------------------------------


def test_parse_parse_manifest_rejects_non_object():
    with pytest.raises(ManifestValidationError, match="manifest must be a JSON object"):
        parse_parse_manifest_dict([])  # type: ignore[arg-type]


def test_parse_parse_manifest_rejects_missing_batch_id():
    raw = _valid_parse_manifest_dict()
    del raw["batch_id"]
    with pytest.raises(ManifestValidationError, match="batch_id"):
        parse_parse_manifest_dict(raw)


def test_parse_parse_manifest_rejects_empty_batch_id():
    with pytest.raises(ManifestValidationError, match="batch_id must be a non-empty"):
        parse_parse_manifest_dict(_valid_parse_manifest_dict(batch_id="   "))


def test_parse_parse_manifest_rejects_missing_urls():
    raw = _valid_parse_manifest_dict()
    del raw["urls"]
    with pytest.raises(ManifestValidationError, match="urls is required"):
        parse_parse_manifest_dict(raw)


def test_parse_parse_manifest_rejects_empty_urls_list():
    with pytest.raises(ManifestValidationError, match="urls must be a non-empty"):
        parse_parse_manifest_dict(_valid_parse_manifest_dict(urls=[]))


def test_parse_parse_manifest_rejects_non_list_urls():
    with pytest.raises(ManifestValidationError, match="urls must be a JSON array"):
        parse_parse_manifest_dict(_valid_parse_manifest_dict(urls="https://example.com"))


def test_parse_parse_manifest_rejects_non_http_scheme():
    """URL 必须 http(s) — 拒 ftp / file / data: / javascript:。"""
    with pytest.raises(ManifestValidationError, match="urls\\[0\\] must start with http"):
        parse_parse_manifest_dict(
            _valid_parse_manifest_dict(urls=["ftp://example.com/x.jpg"])
        )


def test_parse_parse_manifest_rejects_data_uri():
    """data: URI(防 base64 内嵌)直接拒。"""
    with pytest.raises(ManifestValidationError, match="urls\\[1\\] must start with http"):
        parse_parse_manifest_dict(
            _valid_parse_manifest_dict(
                urls=[
                    "https://example.com/x.jpg",
                    "data:image/png;base64,iVBORw0KGgo=",
                ]
            )
        )


def test_parse_parse_manifest_rejects_non_string_url():
    with pytest.raises(ManifestValidationError, match="urls\\[0\\] must be a string"):
        parse_parse_manifest_dict(_valid_parse_manifest_dict(urls=[42]))


def test_parse_parse_manifest_rejects_empty_string_url():
    with pytest.raises(ManifestValidationError, match="urls\\[0\\] must be non-empty"):
        parse_parse_manifest_dict(_valid_parse_manifest_dict(urls=[""]))


# ---------------------------------------------------------------------------
# ImageFetchManifest 正常路径
# ---------------------------------------------------------------------------


def test_parse_fetch_manifest_local_mode_returns_dataclass():
    m = parse_fetch_manifest_dict(_valid_fetch_manifest_dict())
    assert isinstance(m, ImageFetchManifest)
    assert m.batch_id == "img-20260902-001"
    assert m.mode == "local"
    assert isinstance(m.download_dir, Path)
    assert m.download_dir == Path("D:/pics/img-20260902-001")
    assert m.oss is None  # local 模式无 oss
    assert len(m.images) == 2
    assert all(isinstance(img, ImageEntry) for img in m.images)


def test_parse_fetch_manifest_oss_mode_returns_dataclass():
    """mode=oss:oss 配置必填,download_dir 必空(互斥)。"""
    raw = _valid_fetch_manifest_dict()
    raw["mode"] = "oss"
    raw["oss"] = _valid_oss_dict()
    del raw["download_dir"]
    m = parse_fetch_manifest_dict(raw)
    assert m.mode == "oss"
    assert m.download_dir is None
    assert isinstance(m.oss, OssConfig)
    assert m.oss.bucket == "my-image-bucket"


def test_image_entry_with_null_dimensions():
    """width / height 可为 None(部分站点拿不到尺寸)。"""
    raw = _valid_fetch_manifest_dict(
        images=[_valid_image_entry_dict(width=None, height=None)]
    )
    m = parse_fetch_manifest_dict(raw)
    assert m.images[0].width is None
    assert m.images[0].height is None


def test_image_entry_dimensions_must_be_positive_int():
    """width / height 给了就必须为正整数。"""
    raw = _valid_fetch_manifest_dict(
        images=[_valid_image_entry_dict(width=0, height=100)]
    )
    with pytest.raises(ManifestValidationError, match="images\\[0\\].width must be"):
        parse_fetch_manifest_dict(raw)


def test_image_entry_negative_dimension_rejected():
    raw = _valid_fetch_manifest_dict(
        images=[_valid_image_entry_dict(width=-100, height=100)]
    )
    with pytest.raises(ManifestValidationError, match="images\\[0\\].width must be"):
        parse_fetch_manifest_dict(raw)


def test_image_entry_non_int_dimension_rejected():
    raw = _valid_fetch_manifest_dict(
        images=[_valid_image_entry_dict(width="100", height=100)]
    )
    with pytest.raises(ManifestValidationError, match="images\\[0\\].width must be"):
        parse_fetch_manifest_dict(raw)


# ---------------------------------------------------------------------------
# ImageFetchManifest mode 互斥 / 必填校验
# ---------------------------------------------------------------------------


def test_parse_fetch_manifest_rejects_missing_mode():
    raw = _valid_fetch_manifest_dict()
    del raw["mode"]
    with pytest.raises(ManifestValidationError, match="mode is required"):
        parse_fetch_manifest_dict(raw)


def test_parse_fetch_manifest_rejects_unknown_mode():
    with pytest.raises(ManifestValidationError, match="mode must be one of"):
        parse_fetch_manifest_dict(_valid_fetch_manifest_dict(mode="s3"))


def test_parse_fetch_manifest_local_mode_requires_download_dir():
    """mode=local 必带 download_dir。"""
    raw = _valid_fetch_manifest_dict()
    del raw["download_dir"]
    with pytest.raises(
        ManifestValidationError, match="download_dir is required when mode='local'"
    ):
        parse_fetch_manifest_dict(raw)


def test_parse_fetch_manifest_oss_mode_requires_oss():
    """mode=oss 必带 oss 配置。"""
    raw = _valid_fetch_manifest_dict()
    raw["mode"] = "oss"
    del raw["download_dir"]
    with pytest.raises(
        ManifestValidationError, match="oss is required when mode='oss'"
    ):
        parse_fetch_manifest_dict(raw)


def test_parse_fetch_manifest_oss_mode_rejects_download_dir():
    """mode=oss 时 download_dir 应为 None(互斥,避免歧义)。"""
    raw = _valid_fetch_manifest_dict()
    raw["mode"] = "oss"
    raw["oss"] = _valid_oss_dict()
    # 保留 download_dir 字段(即使是空字符串),应该被拒
    raw["download_dir"] = "D:/pics/should_not_be_here"
    with pytest.raises(
        ManifestValidationError, match="download_dir must be null when mode='oss'"
    ):
        parse_fetch_manifest_dict(raw)


def test_parse_fetch_manifest_local_mode_rejects_oss():
    """mode=local 时 oss 应为 None(互斥)。"""
    raw = _valid_fetch_manifest_dict()
    raw["oss"] = _valid_oss_dict()
    with pytest.raises(
        ManifestValidationError, match="oss must be null when mode='local'"
    ):
        parse_fetch_manifest_dict(raw)


def test_parse_fetch_manifest_oss_mode_rejects_invalid_oss():
    """mode=oss:oss 字段错(provider 不在白名单)→ 抛 ManifestValidationError。"""
    raw = _valid_fetch_manifest_dict()
    raw["mode"] = "oss"
    raw["oss"] = _valid_oss_dict()
    raw["oss"]["provider"] = "aws_s3"
    del raw["download_dir"]
    with pytest.raises(ManifestValidationError, match="oss.provider must be one of"):
        parse_fetch_manifest_dict(raw)


# ---------------------------------------------------------------------------
# ImageFetchManifest images 校验
# ---------------------------------------------------------------------------


def test_parse_fetch_manifest_rejects_missing_images():
    raw = _valid_fetch_manifest_dict()
    del raw["images"]
    with pytest.raises(ManifestValidationError, match="images is required"):
        parse_fetch_manifest_dict(raw)


def test_parse_fetch_manifest_rejects_empty_images_list():
    with pytest.raises(ManifestValidationError, match="images must be a non-empty"):
        parse_fetch_manifest_dict(_valid_fetch_manifest_dict(images=[]))


def test_parse_fetch_manifest_rejects_non_list_images():
    with pytest.raises(ManifestValidationError, match="images must be a JSON array"):
        parse_fetch_manifest_dict(_valid_fetch_manifest_dict(images="oops"))


def test_parse_fetch_manifest_rejects_image_entry_missing_url():
    raw = _valid_fetch_manifest_dict()
    del raw["images"][0]["url"]
    with pytest.raises(ManifestValidationError, match="images\\[0\\].url is required"):
        parse_fetch_manifest_dict(raw)


def test_parse_fetch_manifest_rejects_image_entry_non_http_url():
    raw = _valid_fetch_manifest_dict(
        images=[_valid_image_entry_dict(url="ftp://example.com/x.jpg")]
    )
    with pytest.raises(ManifestValidationError, match="must start with http"):
        parse_fetch_manifest_dict(raw)


def test_parse_fetch_manifest_rejects_image_entry_missing_source_url():
    raw = _valid_fetch_manifest_dict()
    del raw["images"][0]["source_url"]
    with pytest.raises(
        ManifestValidationError, match="images\\[0\\].source_url is required"
    ):
        parse_fetch_manifest_dict(raw)


def test_parse_fetch_manifest_rejects_image_entry_non_object():
    raw = _valid_fetch_manifest_dict(images=["https://example.com/x.jpg"])
    with pytest.raises(ManifestValidationError, match="images\\[0\\] must be a JSON object"):
        parse_fetch_manifest_dict(raw)


# ---------------------------------------------------------------------------
# JSON 文件加载
# ---------------------------------------------------------------------------


def test_load_parse_manifest_from_file(tmp_path: Path):
    p = tmp_path / "image_parse_manifest.json"
    p.write_text(json.dumps(_valid_parse_manifest_dict()), encoding="utf-8")
    m = load_parse_manifest(p)
    assert m.batch_id == "img-20260902-001"
    assert len(m.urls) == 2


def test_load_fetch_manifest_from_file(tmp_path: Path):
    p = tmp_path / "image_fetch_manifest.json"
    p.write_text(json.dumps(_valid_fetch_manifest_dict()), encoding="utf-8")
    m = load_fetch_manifest(p)
    assert m.batch_id == "img-20260902-001"
    assert m.mode == "local"
    assert len(m.images) == 2


def test_load_parse_manifest_raises_on_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_parse_manifest(tmp_path / "does_not_exist.json")


def test_load_fetch_manifest_raises_on_invalid_json(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        load_fetch_manifest(p)

