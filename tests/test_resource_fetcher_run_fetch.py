"""test_resource_fetcher_run_fetch — run_fetch 编排层测试.

权威定义: docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md §4 + §5.1 + §7 + §9.
phase: 2026-09-03-universal-resource-fetch Phase 1 Commit 3.

测试用 monkeypatch 把 resolver.fetch + OssUploader 替换为可控 fake, 验证:
  - 单 item 调对应 resolver (按 resource_type 选)
  - 三种 mode (local/oss/both) 事件协议
  - both 模式 OSS 失败 → success + oss_error 字段
  - oss-only 模式 OSS 失败 → failed
  - 取消 (threading.Event)
  - event_cb 缺失时静默
  - 部分成功 (批内一个失败, 其他正常)
  - items 列表顺序保持
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from multimedia_parsing.resource_fetcher.base import (
    FetchDestination,
    FetchResult,
    ResourceItem,
)
from multimedia_parsing.resource_fetcher.manifest import (
    ResourceFetchItem,
    parse_resource_fetch_manifest_dict,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _oss_config() -> Any:
    from multimedia_parsing.video_fetcher.manifest import OssConfig

    return OssConfig(
        provider="aliyun_oss",
        endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        region="cn-hangzhou",
        bucket="b",
        access_key_id="k",
        secret_access_key="s",
    )


@dataclass
class _FakeResolver:
    """占位 Resolver — fetch() 返可控 FetchResult, parse() 抛 NotImplementedError."""

    fetch_results: Dict[str, FetchResult]  # item_id -> FetchResult

    def parse(self, url):  # pragma: no cover — run_fetch 不调
        raise NotImplementedError

    def fetch(self, item: ResourceItem, dest: FetchDestination) -> FetchResult:
        return self.fetch_results.get(item.item_id) or FetchResult(
            item_id=item.item_id,
            resource_type=item.resource_type,
            platform=item.platform,
            error="fake resolver missing fetch result for item",
        )


def _video_item(item_id: str = "bilibili_BV1xx") -> ResourceItem:
    return ResourceItem(
        item_id=item_id,
        resource_type="video",
        platform="bilibili",
        source_url="https://www.bilibili.com/video/BV1xx",
        meta={"video_id": "BV1xx"},
    )


def _image_item(item_id: str = "img_1") -> ResourceItem:
    return ResourceItem(
        item_id=item_id,
        resource_type="image",
        platform="weibo",
        source_url="https://weibo.com/123",
        meta={"image_url": "https://weibo.com/1.jpg"},
    )


# ---------------------------------------------------------------------------
# 基础 — 单 item, mode=local
# ---------------------------------------------------------------------------


def test_run_fetch_local_mode_emits_success(monkeypatch, tmp_path):
    from multimedia_parsing.resource_fetcher import run_fetch as run_fetch_mod

    fake_video = _FakeResolver(
        fetch_results={
            "bilibili_BV1xx": FetchResult(
                item_id="bilibili_BV1xx",
                resource_type="video",
                platform="bilibili",
                local_path=Path("v.mp4"),
            )
        }
    )
    monkeypatch.setattr(
        run_fetch_mod,
        "_select_resolver_for_resource_type",
        lambda rt: fake_video if rt == "video" else None,
    )

    events: List[Dict] = []
    manifest = parse_resource_fetch_manifest_dict(
        {
            "batch_id": "b1",
            "mode": "local",
            "download_dir": str(tmp_path),
            "items": [
                {
                    "url": "https://www.bilibili.com/video/BV1xx",
                    "source_url": "https://www.bilibili.com/video/BV1xx",
                    "resource_type": "video",
                    "item_id": "bilibili_BV1xx",
                    "platform": "bilibili",
                }
            ],
        }
    )
    results = run_fetch_mod.run_fetch(manifest, event_cb=events.append)

    assert len(results) == 1
    assert results[0].is_success
    # event sequence: processing → success
    assert [e["status"] for e in events] == ["processing", "success"]
    assert events[0]["stage"] == "download"
    assert events[1]["stage"] == "download"
    assert events[1]["resource_type"] == "video"
    assert events[1]["item_id"] == "bilibili_BV1xx"
    # result 字段含 local_path (Windows 下 Path("/tmp/v.mp4") 会变 "\tmp\v.mp4",
    # 用 "v.mp4" 兼容跨平台, 验证 path 透传就行)
    assert events[1]["result"]["local_path"] == "v.mp4"


# ---------------------------------------------------------------------------
# mode=oss + both
# ---------------------------------------------------------------------------


def test_run_fetch_oss_mode_emits_success_with_oss_url(monkeypatch, tmp_path):
    from multimedia_parsing.resource_fetcher import run_fetch as run_fetch_mod

    fake_video = _FakeResolver(
        fetch_results={
            "bilibili_BV1xx": FetchResult(
                item_id="bilibili_BV1xx",
                resource_type="video",
                platform="bilibili",
                local_path=Path("/tmp/v.mp4"),
                oss_url="https://oss.example.com/v.mp4",
            )
        }
    )
    monkeypatch.setattr(
        run_fetch_mod,
        "_select_resolver_for_resource_type",
        lambda rt: fake_video if rt == "video" else None,
    )

    events: List[Dict] = []
    manifest = parse_resource_fetch_manifest_dict(
        {
            "batch_id": "b1",
            "mode": "oss",
            "oss": {
                "provider": "aliyun_oss",
                "endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
                "region": "cn-hangzhou",
                "bucket": "b",
                "access_key_id": "k",
                "secret_access_key": "s",
            },
            "items": [
                {
                    "url": "https://www.bilibili.com/video/BV1xx",
                    "source_url": "https://www.bilibili.com/video/BV1xx",
                    "resource_type": "video",
                    "item_id": "bilibili_BV1xx",
                    "platform": "bilibili",
                }
            ],
        }
    )
    results = run_fetch_mod.run_fetch(manifest, event_cb=events.append)

    assert results[0].is_success
    success = next(e for e in events if e["status"] == "success")
    assert success["result"]["oss_url"] == "https://oss.example.com/v.mp4"


def test_run_fetch_both_mode_oss_fail_emits_success_with_message(monkeypatch, tmp_path):
    """spec §9: both 模式 OSS 失败 → 整体 success + result.oss_error."""
    from multimedia_parsing.resource_fetcher import run_fetch as run_fetch_mod

    fake_video = _FakeResolver(
        fetch_results={
            "bilibili_BV1xx": FetchResult(
                item_id="bilibili_BV1xx",
                resource_type="video",
                platform="bilibili",
                local_path=Path("/tmp/v.mp4"),
                oss_url=None,
                oss_error="OSS upload failed: 403",
            )
        }
    )
    monkeypatch.setattr(
        run_fetch_mod,
        "_select_resolver_for_resource_type",
        lambda rt: fake_video if rt == "video" else None,
    )

    events: List[Dict] = []
    manifest = parse_resource_fetch_manifest_dict(
        {
            "batch_id": "b1",
            "mode": "both",
            "download_dir": str(tmp_path),
            "oss": {
                "provider": "aliyun_oss",
                "endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
                "region": "cn-hangzhou",
                "bucket": "b",
                "access_key_id": "k",
                "secret_access_key": "s",
            },
            "items": [
                {
                    "url": "https://www.bilibili.com/video/BV1xx",
                    "source_url": "https://www.bilibili.com/video/BV1xx",
                    "resource_type": "video",
                    "item_id": "bilibili_BV1xx",
                    "platform": "bilibili",
                }
            ],
        }
    )
    results = run_fetch_mod.run_fetch(manifest, event_cb=events.append)

    # 整体 success
    assert results[0].is_success
    # success 事件里 result 字段含 oss_error
    success = next(e for e in events if e["status"] == "success")
    assert "oss_error" in success["result"]
    assert "403" in success["result"]["oss_error"]


def test_run_fetch_oss_mode_oss_fail_emits_failed(monkeypatch, tmp_path):
    """spec §9: oss-only 模式 OSS 失败 → 整体 failed (无本地兜底)."""
    from multimedia_parsing.resource_fetcher import run_fetch as run_fetch_mod

    fake_video = _FakeResolver(
        fetch_results={
            "bilibili_BV1xx": FetchResult(
                item_id="bilibili_BV1xx",
                resource_type="video",
                platform="bilibili",
                local_path=Path("/tmp/v.mp4"),
                error="upload: OSS 403",
            )
        }
    )
    monkeypatch.setattr(
        run_fetch_mod,
        "_select_resolver_for_resource_type",
        lambda rt: fake_video if rt == "video" else None,
    )

    events: List[Dict] = []
    manifest = parse_resource_fetch_manifest_dict(
        {
            "batch_id": "b1",
            "mode": "oss",
            "oss": {
                "provider": "aliyun_oss",
                "endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
                "region": "cn-hangzhou",
                "bucket": "b",
                "access_key_id": "k",
                "secret_access_key": "s",
            },
            "items": [
                {
                    "url": "https://www.bilibili.com/video/BV1xx",
                    "source_url": "https://www.bilibili.com/video/BV1xx",
                    "resource_type": "video",
                    "item_id": "bilibili_BV1xx",
                    "platform": "bilibili",
                }
            ],
        }
    )
    results = run_fetch_mod.run_fetch(manifest, event_cb=events.append)

    assert not results[0].is_success
    failed = next(e for e in events if e["status"] == "failed")
    assert "OSS 403" in failed["error"]


# ---------------------------------------------------------------------------
# 资源类型路由
# ---------------------------------------------------------------------------


def test_run_fetch_unsupported_resource_type_emits_failed(monkeypatch, tmp_path):
    """D2 预留: audio / model 路由到 resource_fetcher 时, video/image resolver 不识别 → failed."""
    from multimedia_parsing.resource_fetcher import run_fetch as run_fetch_mod

    # audio 在 RESOLVER_RULES 里没注册 — _select_resolver_for_resource_type 返 None
    monkeypatch.setattr(
        run_fetch_mod,
        "_select_resolver_for_resource_type",
        lambda rt: None,
    )

    events: List[Dict] = []
    manifest = parse_resource_fetch_manifest_dict(
        {
            "batch_id": "b1",
            "mode": "local",
            "download_dir": str(tmp_path),
            "items": [
                {
                    "url": "https://example.com/audio.mp3",
                    "source_url": "https://example.com/audio.mp3",
                    "resource_type": "audio",  # D2 预留, 本期无 resolver
                    "item_id": "audio_1",
                    "platform": "unknown",
                }
            ],
        }
    )
    results = run_fetch_mod.run_fetch(manifest, event_cb=events.append)

    assert not results[0].is_success
    assert "audio" in results[0].error or "unsupported" in results[0].error
    failed = next(e for e in events if e["status"] == "failed")


def test_run_fetch_image_resource_routes_to_image_resolver(monkeypatch, tmp_path):
    """resource_type=image 走 ImageResolver, 跟 video 分开."""
    from multimedia_parsing.resource_fetcher import run_fetch as run_fetch_mod

    fake_image = _FakeResolver(
        fetch_results={
            "img_1": FetchResult(
                item_id="img_1",
                resource_type="image",
                platform="weibo",
                local_path=Path("/tmp/01_x.jpg"),
            )
        }
    )
    fake_video = _FakeResolver(
        fetch_results={
            "vid_1": FetchResult(
                item_id="vid_1",
                resource_type="video",
                platform="bilibili",
                local_path=Path("/tmp/v.mp4"),
            )
        }
    )

    def _select(rt):
        return {"video": fake_video, "image": fake_image}.get(rt)

    monkeypatch.setattr(run_fetch_mod, "_select_resolver_for_resource_type", _select)

    events: List[Dict] = []
    manifest = parse_resource_fetch_manifest_dict(
        {
            "batch_id": "b1",
            "mode": "local",
            "download_dir": str(tmp_path),
            "items": [
                {
                    "url": "https://www.bilibili.com/video/BV1",
                    "source_url": "https://www.bilibili.com/video/BV1",
                    "resource_type": "video",
                    "item_id": "vid_1",
                    "platform": "bilibili",
                },
                {
                    "url": "https://weibo.com/1.jpg",
                    "source_url": "https://weibo.com/123",
                    "resource_type": "image",
                    "item_id": "img_1",
                    "platform": "weibo",
                },
            ],
        }
    )
    results = run_fetch_mod.run_fetch(manifest, event_cb=events.append)

    assert len(results) == 2
    assert results[0].item_id == "vid_1"  # 顺序保持
    assert results[1].item_id == "img_1"
    # image 用了 image resolver, 路径有 .jpg
    assert results[1].local_path == Path("/tmp/01_x.jpg")


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------


def test_run_fetch_cancel_mid_batch(monkeypatch, tmp_path):
    from multimedia_parsing.resource_fetcher import run_fetch as run_fetch_mod

    fake = _FakeResolver(
        fetch_results={
            "item_1": FetchResult(
                item_id="item_1",
                resource_type="video",
                platform="bilibili",
                local_path=Path("/tmp/1.mp4"),
            ),
            "item_2": FetchResult(
                item_id="item_2",
                resource_type="video",
                platform="bilibili",
                local_path=Path("/tmp/2.mp4"),
            ),
            "item_3": FetchResult(
                item_id="item_3",
                resource_type="video",
                platform="bilibili",
                local_path=Path("/tmp/3.mp4"),
            ),
        }
    )
    monkeypatch.setattr(
        run_fetch_mod,
        "_select_resolver_for_resource_type",
        lambda rt: fake,
    )

    cancel_event = threading.Event()
    original_fetch = fake.fetch

    def fetch_with_cancel(item, dest):
        if item.item_id == "item_2":
            cancel_event.set()
        return original_fetch(item, dest)

    fake.fetch = fetch_with_cancel

    manifest = parse_resource_fetch_manifest_dict(
        {
            "batch_id": "b1",
            "mode": "local",
            "download_dir": str(tmp_path),
            "items": [
                {
                    "url": "https://www.bilibili.com/video/1",
                    "source_url": "https://www.bilibili.com/video/1",
                    "resource_type": "video",
                    "item_id": "item_1",
                    "platform": "bilibili",
                },
                {
                    "url": "https://www.bilibili.com/video/2",
                    "source_url": "https://www.bilibili.com/video/2",
                    "resource_type": "video",
                    "item_id": "item_2",
                    "platform": "bilibili",
                },
                {
                    "url": "https://www.bilibili.com/video/3",
                    "source_url": "https://www.bilibili.com/video/3",
                    "resource_type": "video",
                    "item_id": "item_3",
                    "platform": "bilibili",
                },
            ],
        }
    )
    results = run_fetch_mod.run_fetch(manifest, event_cb=lambda e: None, cancel_event=cancel_event)

    # item_1 处理, item_2 处理时 set cancel, item_3 不入 results
    assert len(results) == 2
    assert results[0].item_id == "item_1"
    assert results[1].item_id == "item_2"


# ---------------------------------------------------------------------------
# 部分成功
# ---------------------------------------------------------------------------


def test_run_fetch_batch_partial_success(monkeypatch, tmp_path):
    """spec §8: 单条失败不影响其他."""
    from multimedia_parsing.resource_fetcher import run_fetch as run_fetch_mod

    fake = _FakeResolver(
        fetch_results={
            "ok_1": FetchResult(
                item_id="ok_1",
                resource_type="video",
                platform="bilibili",
                local_path=Path("/tmp/1.mp4"),
            ),
            "fail_1": FetchResult(
                item_id="fail_1",
                resource_type="video",
                platform="bilibili",
                error="network timeout",
            ),
            "ok_2": FetchResult(
                item_id="ok_2",
                resource_type="video",
                platform="bilibili",
                local_path=Path("/tmp/2.mp4"),
            ),
        }
    )
    monkeypatch.setattr(
        run_fetch_mod,
        "_select_resolver_for_resource_type",
        lambda rt: fake,
    )

    events: List[Dict] = []
    manifest = parse_resource_fetch_manifest_dict(
        {
            "batch_id": "b1",
            "mode": "local",
            "download_dir": str(tmp_path),
            "items": [
                {
                    "url": "https://www.bilibili.com/video/1",
                    "source_url": "https://www.bilibili.com/video/1",
                    "resource_type": "video",
                    "item_id": "ok_1",
                    "platform": "bilibili",
                },
                {
                    "url": "https://www.bilibili.com/video/2",
                    "source_url": "https://www.bilibili.com/video/2",
                    "resource_type": "video",
                    "item_id": "fail_1",
                    "platform": "bilibili",
                },
                {
                    "url": "https://www.bilibili.com/video/3",
                    "source_url": "https://www.bilibili.com/video/3",
                    "resource_type": "video",
                    "item_id": "ok_2",
                    "platform": "bilibili",
                },
            ],
        }
    )
    results = run_fetch_mod.run_fetch(manifest, event_cb=events.append)

    assert len(results) == 3
    assert results[0].is_success
    assert not results[1].is_success
    assert "timeout" in results[1].error
    assert results[2].is_success
    # 事件 sequence: 1 success, 1 failed, 1 success
    statuses = [e["status"] for e in events if e["status"] in ("success", "failed")]
    assert statuses == ["success", "failed", "success"]


# ---------------------------------------------------------------------------
# event_cb 缺失
# ---------------------------------------------------------------------------


def test_run_fetch_no_event_cb_silent(monkeypatch, tmp_path):
    from multimedia_parsing.resource_fetcher import run_fetch as run_fetch_mod

    fake = _FakeResolver(
        fetch_results={
            "x": FetchResult(
                item_id="x",
                resource_type="video",
                platform="bilibili",
                local_path=Path("/tmp/x.mp4"),
            )
        }
    )
    monkeypatch.setattr(
        run_fetch_mod,
        "_select_resolver_for_resource_type",
        lambda rt: fake,
    )

    manifest = parse_resource_fetch_manifest_dict(
        {
            "batch_id": "b1",
            "mode": "local",
            "download_dir": str(tmp_path),
            "items": [
                {
                    "url": "https://www.bilibili.com/video/x",
                    "source_url": "https://www.bilibili.com/video/x",
                    "resource_type": "video",
                    "item_id": "x",
                    "platform": "bilibili",
                }
            ],
        }
    )
    results = run_fetch_mod.run_fetch(manifest)
    assert len(results) == 1
    assert results[0].is_success


# ---------------------------------------------------------------------------
# 编排层不调 parse
# ---------------------------------------------------------------------------


def test_run_fetch_does_not_call_parse(monkeypatch, tmp_path):
    from multimedia_parsing.resource_fetcher import run_fetch as run_fetch_mod

    fake = _FakeResolver(
        fetch_results={
            "x": FetchResult(
                item_id="x",
                resource_type="video",
                platform="bilibili",
                local_path=Path("/tmp/x.mp4"),
            )
        }
    )
    monkeypatch.setattr(
        run_fetch_mod,
        "_select_resolver_for_resource_type",
        lambda rt: fake,
    )

    manifest = parse_resource_fetch_manifest_dict(
        {
            "batch_id": "b1",
            "mode": "local",
            "download_dir": str(tmp_path),
            "items": [
                {
                    "url": "https://www.bilibili.com/video/x",
                    "source_url": "https://www.bilibili.com/video/x",
                    "resource_type": "video",
                    "item_id": "x",
                    "platform": "bilibili",
                }
            ],
        }
    )
    run_fetch_mod.run_fetch(manifest, event_cb=lambda e: None)
    # parse 内部抛 NotImplementedError — 不应被调用
    # 这里没有直接断言, 但如果 fetch 也被调, fetch_results["x"] 应被消费

