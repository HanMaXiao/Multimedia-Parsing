"""test_video_fetcher_manifest.py — manifest.py 单元测试。

覆盖范围:
  - 合法 manifest 解析成功
  - 缺字段 / 类型错 / 边界不通过 → ManifestValidationError
  - OssConfig.__post_init__ 防御(provider 白名单 / 必填字段)
  - 路径字段自动转 Path
  - 可选字段(None / 缺省)默认值
  - 嵌套 OssConfig 错(JSON object 误传 list / 字符串)
  - JSON 文件加载(load_manifest)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from multimedia_parsing.video_fetcher.manifest import (
    ManifestValidationError,
    OssConfig,
    VALID_PROVIDERS,
    VideoFetchManifest,
    load_manifest,
    parse_manifest_dict,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _valid_oss_dict() -> dict:
    return {
        "provider": "aliyun_oss",
        "endpoint": "oss-cn-hangzhou.aliyuncs.com",
        "region": "cn-hangzhou",
        "bucket": "my-video-bucket",
        "access_key_id": "AKID",
        "secret_access_key": "SK",
        "path_prefix": "videos",
        "public_base_url": "https://cdn.example.com",
    }


def _valid_manifest_dict(**overrides) -> dict:
    base = {
        "batch_id": "batch_2026_09_02_001",
        "urls": [
            "https://www.bilibili.com/video/BV1YM4m1z7nB",
            "https://www.bilibili.com/video/BV1AbCdEfGhI",
        ],
        "oss": _valid_oss_dict(),
        "download_dir": "D:/runs/batch_001/downloads",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


def test_parse_valid_manifest_returns_dataclass():
    m = parse_manifest_dict(_valid_manifest_dict())
    assert isinstance(m, VideoFetchManifest)
    assert m.batch_id == "batch_2026_09_02_001"
    assert len(m.urls) == 2
    assert m.urls[0].startswith("https://www.bilibili.com")
    assert isinstance(m.oss, OssConfig)
    assert isinstance(m.download_dir, Path)
    assert m.download_dir == Path("D:/runs/batch_001/downloads")
    # 可选字段默认值
    assert m.cookie_file is None
    assert m.account_id is None
    assert m.platform_filter == []


def test_oss_config_defaults():
    """OssConfig 缺省 path_prefix / public_base_url 走默认值。"""
    m = parse_manifest_dict(_valid_manifest_dict())
    assert m.oss.path_prefix == "videos"
    assert m.oss.public_base_url == "https://cdn.example.com"


def test_oss_config_optional_path_prefix_default_empty_string():
    """path_prefix 缺省时是空串(走 default='' 不是 None,避免 boto3 上传路径拼接坑)。"""
    oss = OssConfig(
        provider="minio",
        endpoint="minio.local:9000",
        region="us-east-1",
        bucket="b",
        access_key_id="ak",
        secret_access_key="sk",
    )
    assert oss.path_prefix == ""
    assert oss.public_base_url is None


def test_parse_manifest_with_all_optional_fields():
    m = parse_manifest_dict(
        _valid_manifest_dict(
            cookie_file="D:/runs/batch_001/cookies.txt",
            account_id="user_001",
            platform_filter=["bilibili", "youtube"],
        )
    )
    assert m.cookie_file == Path("D:/runs/batch_001/cookies.txt")
    assert m.account_id == "user_001"
    assert m.platform_filter == ["bilibili", "youtube"]


def test_valid_providers_includes_documented_set():
    """D3 决策:5 个 provider 覆盖国内云 + 国外 + 自定义。"""
    assert "aliyun_oss" in VALID_PROVIDERS
    assert "tencent_cos" in VALID_PROVIDERS
    assert "cloudflare_r2" in VALID_PROVIDERS
    assert "minio" in VALID_PROVIDERS
    assert "custom" in VALID_PROVIDERS


# ---------------------------------------------------------------------------
# OssConfig 边界
# ---------------------------------------------------------------------------


def test_oss_provider_must_be_in_whitelist():
    with pytest.raises(ManifestValidationError, match="oss.provider must be one of"):
        OssConfig(
            provider="aws_s3",  # 不在白名单 — 我们走 S3 兼容,不需要 aws 原生
            endpoint="s3.amazonaws.com",
            region="us-east-1",
            bucket="b",
            access_key_id="ak",
            secret_access_key="sk",
        )


def test_oss_endpoint_must_be_non_empty():
    with pytest.raises(ManifestValidationError, match="oss.endpoint must be non-empty"):
        OssConfig(
            provider="minio",
            endpoint="",
            region="r",
            bucket="b",
            access_key_id="ak",
            secret_access_key="sk",
        )


def test_oss_bucket_must_be_non_empty():
    with pytest.raises(ManifestValidationError, match="oss.bucket must be non-empty"):
        OssConfig(
            provider="minio",
            endpoint="e",
            region="r",
            bucket="",
            access_key_id="ak",
            secret_access_key="sk",
        )


def test_oss_ak_sk_must_be_non_empty():
    """AK/SK 空 → 上传时无认证,必须 fail-fast。"""
    with pytest.raises(ManifestValidationError, match="access_key_id"):
        OssConfig(
            provider="minio",
            endpoint="e",
            region="r",
            bucket="b",
            access_key_id="",
            secret_access_key="sk",
        )
    with pytest.raises(ManifestValidationError, match="secret_access_key"):
        OssConfig(
            provider="minio",
            endpoint="e",
            region="r",
            bucket="b",
            access_key_id="ak",
            secret_access_key="",
        )


# ---------------------------------------------------------------------------
# 缺字段 / 类型错
# ---------------------------------------------------------------------------


def test_parse_rejects_non_object():
    with pytest.raises(ManifestValidationError, match="manifest must be a JSON object"):
        parse_manifest_dict([])  # type: ignore[arg-type]


def test_parse_rejects_missing_batch_id():
    raw = _valid_manifest_dict()
    del raw["batch_id"]
    with pytest.raises(ManifestValidationError, match="batch_id"):
        parse_manifest_dict(raw)


def test_parse_rejects_empty_batch_id():
    raw = _valid_manifest_dict(batch_id="   ")
    with pytest.raises(ManifestValidationError, match="batch_id must be a non-empty"):
        parse_manifest_dict(raw)


def test_parse_rejects_missing_urls():
    raw = _valid_manifest_dict()
    del raw["urls"]
    with pytest.raises(ManifestValidationError, match="urls is required"):
        parse_manifest_dict(raw)


def test_parse_rejects_empty_urls_list():
    with pytest.raises(ManifestValidationError, match="urls must be a non-empty"):
        parse_manifest_dict(_valid_manifest_dict(urls=[]))


def test_parse_rejects_non_list_urls():
    with pytest.raises(ManifestValidationError, match="urls must be a JSON array"):
        parse_manifest_dict(_valid_manifest_dict(urls="https://example.com"))


def test_parse_rejects_url_with_empty_string():
    with pytest.raises(ManifestValidationError, match="urls\\[0\\] must be non-empty"):
        parse_manifest_dict(_valid_manifest_dict(urls=["", "https://example.com/x"]))


def test_parse_rejects_url_with_non_string_item():
    with pytest.raises(ManifestValidationError, match="urls\\[1\\] must be a string"):
        parse_manifest_dict(_valid_manifest_dict(urls=["https://x", 42]))


def test_parse_oss_optional_when_missing_uses_local_mode():
    """F5:oss 变 optional — 缺省 = 本地下载模式(Plan follow-up 5/6)。

    之前:缺 oss 抛 ManifestValidationError。现在:None = 本地模式,run_fetch
    跳过 upload,success 状态用 `file://` 本地路径。
    """
    raw = _valid_manifest_dict()
    del raw["oss"]
    m = parse_manifest_dict(raw)
    assert m.oss is None, "缺 oss 字段必返 None(本地模式)"


def test_parse_oss_optional_when_explicit_none_uses_local_mode():
    raw = _valid_manifest_dict()
    raw["oss"] = None
    m = parse_manifest_dict(raw)
    assert m.oss is None, "oss: null 也必视作 None(本地模式)"


def test_parse_still_rejects_invalid_oss_block_when_present():
    """F5:oss 缺省 = OK,给但字段错(类型 / 缺 key)仍要拒。"""
    raw = _valid_manifest_dict()
    raw["oss"] = {"provider": "aliyun_oss"}  # 缺 5 个必填
    with pytest.raises(ManifestValidationError, match="oss missing required fields"):
        parse_manifest_dict(raw)


def test_parse_rejects_oss_not_object():
    with pytest.raises(ManifestValidationError, match="oss must be a JSON object"):
        parse_manifest_dict(_valid_manifest_dict(oss="oops"))


def test_parse_rejects_oss_missing_access_key_id():
    oss = _valid_oss_dict()
    del oss["access_key_id"]
    with pytest.raises(ManifestValidationError, match="oss missing required fields"):
        parse_manifest_dict(_valid_manifest_dict(oss=oss))


def test_parse_rejects_missing_download_dir():
    raw = _valid_manifest_dict()
    del raw["download_dir"]
    with pytest.raises(ManifestValidationError, match="download_dir is required"):
        parse_manifest_dict(raw)


def test_parse_rejects_empty_download_dir():
    with pytest.raises(ManifestValidationError, match="download_dir must be a non-empty"):
        parse_manifest_dict(_valid_manifest_dict(download_dir=""))


def test_parse_rejects_invalid_optional_path():
    with pytest.raises(ManifestValidationError, match="cookie_file must be a non-empty string or null"):
        parse_manifest_dict(_valid_manifest_dict(cookie_file=123))


def test_parse_rejects_invalid_platform_filter():
    with pytest.raises(ManifestValidationError, match="platform_filter must be a JSON array"):
        parse_manifest_dict(_valid_manifest_dict(platform_filter="bilibili"))


def test_parse_rejects_empty_string_in_platform_filter():
    with pytest.raises(ManifestValidationError, match="platform_filter\\[1\\] must be a non-empty"):
        parse_manifest_dict(_valid_manifest_dict(platform_filter=["bilibili", "  "]))


# ---------------------------------------------------------------------------
# JSON 文件加载
# ---------------------------------------------------------------------------


def test_load_manifest_from_file(tmp_path: Path):
    manifest_path = tmp_path / "fetch_manifest.json"
    manifest_path.write_text(
        json.dumps(_valid_manifest_dict()), encoding="utf-8"
    )
    m = load_manifest(manifest_path)
    assert m.batch_id == "batch_2026_09_02_001"
    assert isinstance(m.download_dir, Path)


def test_load_manifest_raises_on_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_manifest(tmp_path / "does_not_exist.json")


def test_load_manifest_raises_on_invalid_json(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        load_manifest(p)

