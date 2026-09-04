"""test_event_emitter_resource_status — emit_resource_fetch_status 单元测试.

权威定义: docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md §5.1.
phase: 2026-09-03-universal-resource-fetch Phase 1 Commit 3.
"""
from __future__ import annotations

import json
from io import StringIO

import pytest

from multimedia_parsing.event_emitter import (
    VALID_PLATFORMS,
    VALID_STATUSES,
    emit_resource_fetch_status,
)


# ---------------------------------------------------------------------------
# 捕获 stdout helper — 用 pytest 内置 capsys (跟现有 test_event_emitter.py 一致)
# ---------------------------------------------------------------------------


def _last_event(capsys) -> dict:
    """从 capsys.readouterr().out 读最后一行 __EVENT__:..."""
    captured = capsys.readouterr()
    content = captured.out.strip()
    if not content:
        raise AssertionError("no __EVENT__: line emitted")
    lines = [l for l in content.split("\n") if l.startswith("__EVENT__:")]
    assert lines, f"no __EVENT__: line in {content!r}"
    last = lines[-1]
    payload_str = last[len("__EVENT__:"):]
    return json.loads(payload_str)


# ---------------------------------------------------------------------------
# 基础字段
# ---------------------------------------------------------------------------


def test_emit_minimal_required_fields(capsys):
    """只传必填 batch_id/stage/status 也应能 emit (其余字段 None 不发)."""
    emit_resource_fetch_status(
        batch_id="b1",
        stage="parse",
        status="processing",
    )
    ev = _last_event(capsys)
    assert ev["type"] == "resource_fetch_status"
    assert ev["batch_id"] == "b1"
    assert ev["stage"] == "parse"
    assert ev["status"] == "processing"
    assert "ts" in ev
    # resource_type / item_id / platform / source_url 都是 None 时不应在 payload
    assert "resource_type" not in ev
    assert "item_id" not in ev
    assert "platform" not in ev
    assert "source_url" not in ev


def test_emit_full_fields(capsys):
    emit_resource_fetch_status(
        batch_id="b1",
        stage="download",
        status="success",
        resource_type="video",
        item_id="bilibili_BV1xx",
        platform="bilibili",
        source_url="https://bilibili.com/video/BV1xx",
        progress=42.0,
        result={"local_path": "/tmp/v.mp4", "oss_url": "https://oss.example.com/x"},
    )
    ev = _last_event(capsys)
    assert ev["resource_type"] == "video"
    assert ev["item_id"] == "bilibili_BV1xx"
    assert ev["platform"] == "bilibili"
    assert ev["source_url"] == "https://bilibili.com/video/BV1xx"
    assert ev["progress"] == 42.0
    assert ev["result"]["local_path"] == "/tmp/v.mp4"


# ---------------------------------------------------------------------------
# 白名单校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage", ["parse", "download", "upload"])
def test_emit_valid_stages(capsys, stage):
    emit_resource_fetch_status(batch_id="b1", stage=stage, status="processing")
    assert _last_event(capsys)["stage"] == stage


@pytest.mark.parametrize("stage", ["PARSE", "fetch", "del", "invalid", ""])
def test_emit_invalid_stage_raises(stage):
    with pytest.raises(ValueError, match="stage must be one of"):
        emit_resource_fetch_status(batch_id="b1", stage=stage, status="processing")


@pytest.mark.parametrize("status", sorted(VALID_STATUSES))
def test_emit_valid_statuses(capsys, status):
    emit_resource_fetch_status(batch_id="b1", stage="parse", status=status)
    assert _last_event(capsys)["status"] == status


def test_emit_invalid_status_raises():
    with pytest.raises(ValueError, match="status must be one of"):
        emit_resource_fetch_status(batch_id="b1", stage="parse", status="not-a-status")


@pytest.mark.parametrize("rtype", ["video", "image", "audio", "model"])
def test_emit_valid_resource_type(capsys, rtype):
    emit_resource_fetch_status(
        batch_id="b1", stage="download", status="success", resource_type=rtype
    )
    assert _last_event(capsys)["resource_type"] == rtype


@pytest.mark.parametrize("rtype", ["music", "doc", "video2", ""])
def test_emit_invalid_resource_type_raises(rtype):
    with pytest.raises(ValueError, match="resource_type must be one of"):
        emit_resource_fetch_status(
            batch_id="b1", stage="download", status="success", resource_type=rtype
        )


# ---------------------------------------------------------------------------
# 必填字段校验
# ---------------------------------------------------------------------------


def test_emit_empty_batch_id_raises():
    with pytest.raises(ValueError, match="batch_id must be non-empty"):
        emit_resource_fetch_status(batch_id="", stage="parse", status="running")


def test_emit_empty_stage_raises():
    with pytest.raises(ValueError, match="stage must be one of"):
        emit_resource_fetch_status(batch_id="b1", stage="", status="running")


def test_emit_empty_status_raises():
    with pytest.raises(ValueError, match="status must be one of"):
        emit_resource_fetch_status(batch_id="b1", stage="parse", status="")


# ---------------------------------------------------------------------------
# 加性字段 (None 不发)
# ---------------------------------------------------------------------------


def test_emit_none_optional_fields_omitted(capsys):
    """spec §5.1: 加性字段 None 时不应出现在 payload 里."""
    emit_resource_fetch_status(
        batch_id="b1",
        stage="download",
        status="processing",
        resource_type="video",
        item_id="x",
        platform="bilibili",
        source_url=None,
        progress=None,
        result=None,
        error=None,
        image_url=None,
        items=None,
    )
    ev = _last_event(capsys)
    assert "source_url" not in ev
    assert "progress" not in ev
    assert "result" not in ev
    assert "error" not in ev
    assert "image_url" not in ev
    assert "items" not in ev


def test_emit_chinese_message_preserved(capsys):
    """ensure_ascii=False — 中文 message / error 不被转义."""
    emit_resource_fetch_status(
        batch_id="b1",
        stage="download",
        status="success",
        resource_type="image",
        item_id="img_1",
        platform="weibo",
        message="本地已保存, OSS 上传失败",
    )
    content = capsys.readouterr().out
    assert "本地已保存, OSS 上传失败" in content
    # ensure_ascii=False 验证: payload 中是 raw 中文, 不是 \uXXXX 转义
    assert r"\u672c\u5730\u5df2\u4fdd\u5b58" not in content


def test_emit_progress_dict_form(capsys):
    """spec §5.1: progress 可以是 {done, total} dict 形式 (image 模块兼容)."""
    emit_resource_fetch_status(
        batch_id="b1",
        stage="download",
        status="processing",
        progress={"done": 3, "total": 10},
    )
    assert _last_event(capsys)["progress"] == {"done": 3, "total": 10}


def test_emit_entry_id_inherited(capsys):
    """emit 行为跟 emit_image_fetch_status 一致: 走 event_emitter module-level entry_id."""
    import event_emitter

    event_emitter.set_current_entry_id("entry-xyz")
    try:
        emit_resource_fetch_status(batch_id="b1", stage="parse", status="processing")
        ev = _last_event(capsys)
        assert ev["entry_id"] == "entry-xyz"
    finally:
        event_emitter.set_current_entry_id(None)


