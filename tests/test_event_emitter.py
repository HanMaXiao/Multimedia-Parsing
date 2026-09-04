"""event_emitter.py 单元测试 — Phase 9 PyTest 收口。

权威规范:
  - docs/SPEC.md §3.3 (9 状态机)
  - docs/SPEC.md §3.5 (IPC 协议:__EVENT__: 前缀 + 单行 JSON)
  - .harness/docs/event-protocol.md §7 (Python emit 工具签名 + 防御校验)

覆盖范围:
  - 9 状态白名单全部合法接受
  - 错别字 status 抛 ValueError(防御边界)
  - 空 article / 空 platform 抛 ValueError
  - 中文 article 走 ensure_ascii=False(SPEC §3.5 Windows GBK 乱码防御)
  - 输出格式严格匹配 "__EVENT__:" + JSON
  - ts 字段 ISO 8601 UTC + 毫秒精度 + Z 后缀
  - url/error/screenshot 默认 None,非空字段正确传递
"""

import json
import re

import pytest

from multimedia_parsing.event_emitter import (
    VALID_PLATFORMS,
    VALID_STATUSES,
    emit_platform_status,
    get_current_entry_id,
    now_iso,
    set_current_entry_id,
)

# ---------------------------------------------------------------------------
# 常量完整性测试
# ---------------------------------------------------------------------------


def test_valid_statuses_has_9_entries():
    """SPEC §3.3 状态机:9 个状态(4 非终态 + 5 终态)。"""
    assert len(VALID_STATUSES) == 9


def test_valid_statuses_contains_required_states():
    """SPEC §3.3 状态机:9 个状态具体值。"""
    required = {
        # 4 非终态
        "pending",
        "logging_in",
        "uploading",
        "processing",
        # 5 终态
        "success",
        "draft",
        "failed",
        "skipped",
        "cancelled",
    }
    assert required == VALID_STATUSES


def test_valid_platforms_includes_spec1_platforms():
    """SPEC §3.1 范围:4 个文章平台。"""
    assert "toutiao" in VALID_PLATFORMS
    assert "csdn" in VALID_PLATFORMS
    assert "zhihu" in VALID_PLATFORMS
    assert "juejin" in VALID_PLATFORMS


def test_valid_platforms_includes_bilibili_for_video_fetch():
    """Plan 2026-09-02-video-fetch-oss:D4 决策,video_fetcher 首期支持 bilibili,
    emit 必须接受 bilibili 作 platform 字段(否则 run_fetch 第一条事件就抛 ValueError)。"""
    assert "bilibili" in VALID_PLATFORMS


# ---------------------------------------------------------------------------
# 防御校验测试(SPEC §5 "Python 端不会出现"是常态,但 emit 入口兜底)
# ---------------------------------------------------------------------------


def test_emit_rejects_empty_article(capsys):
    """空 article 抛 ValueError + 提示信息明确。"""
    with pytest.raises(ValueError, match="article must be non-empty"):
        emit_platform_status("", "toutiao", "uploading")


def test_emit_rejects_empty_platform(capsys):
    """空 platform 抛 ValueError。"""
    with pytest.raises(ValueError, match="platform must be non-empty"):
        emit_platform_status("article.md", "", "uploading")


def test_emit_rejects_unknown_status(capsys):
    """错别字 status 抛 ValueError + 列出 9 合法值(便于 debug)。"""
    with pytest.raises(ValueError, match="unknown status 'wrong_typo'"):
        emit_platform_status("article.md", "toutiao", "wrong_typo")


@pytest.mark.parametrize(
    "invalid_status",
    ["PENDING", "Success", "successs", "drafts", "中", ""],
)
def test_emit_rejects_various_invalid_statuses(invalid_status):
    """各种错别字 / 大小写错 / 空 status 都拒收。"""
    if invalid_status == "":
        # 空字符串走的是 status not in VALID_STATUSES 路径
        # 但 article="article.md" / platform="toutiao" 通过,只是 status 错
        with pytest.raises(ValueError, match="unknown status"):
            emit_platform_status("article.md", "toutiao", invalid_status)
    else:
        with pytest.raises(ValueError):
            emit_platform_status("article.md", "toutiao", invalid_status)


# ---------------------------------------------------------------------------
# 输出格式测试(SPEC §3.5 __EVENT__: 前缀 + 单行 JSON)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", sorted(VALID_STATUSES))
def test_emit_writes_event_prefix_and_valid_json(status, capsys):
    """9 个合法 status 都输出 `__EVENT__:` + 合法 JSON 到 stdout。"""
    emit_platform_status("article.md", "toutiao", status)

    captured = capsys.readouterr()
    lines = [l for l in captured.out.splitlines() if l.startswith("__EVENT__:")]
    assert len(lines) == 1, f"expected 1 __EVENT__ line, got {len(lines)}: {lines!r}"

    # JSON 合法
    json_str = lines[0][len("__EVENT__:") :]
    payload = json.loads(json_str)

    # 字段必带
    assert payload["type"] == "platform_status"
    assert payload["article"] == "article.md"
    assert payload["platform"] == "toutiao"
    assert payload["status"] == status
    assert payload["url"] is None
    assert payload["error"] is None
    assert payload["screenshot"] is None
    assert isinstance(payload["ts"], str)


def test_emit_chinese_article_preserves_chinese(capsys):
    """SPEC §3.5 关键:ensure_ascii=False 保留中文(article 名经常含中文)。"""
    emit_platform_status("中文测试文章.md", "toutiao", "uploading")

    captured = capsys.readouterr()
    line = [l for l in captured.out.splitlines() if l.startswith("__EVENT__:")][0]

    # stdout 输出含中文(不是 \\u 转义)
    assert "中文测试文章.md" in line, f"expected Chinese in stdout, got: {line!r}"
    # 反向:不应该有 unicode escape
    assert "\\u" not in line


def test_emit_payload_keys_match_spec(capsys):
    """SPEC §3.5 + event-protocol.md §2 schema:9 个字段(Phase 30a Spec 5 加 entry_id)。"""
    emit_platform_status(
        "article.md",
        "toutiao",
        "success",
        url="https://example.com/x",
    )
    captured = capsys.readouterr()
    line = [l for l in captured.out.splitlines() if l.startswith("__EVENT__:")][0]
    payload = json.loads(line[len("__EVENT__:") :])

    expected_keys = {
        "type",
        "entry_id",  # Phase 30a Spec 5:多 entry 并行路由 key
        "article",
        "platform",
        "status",
        "url",
        "error",
        "screenshot",
        "ts",
    }
    assert set(payload.keys()) == expected_keys


# ---------------------------------------------------------------------------
# Plan 2026-09-02-video-fetch-oss F3.3:加性字段 source_url / progress
# ---------------------------------------------------------------------------
# spec §5 表格"加性扩展":Rust 透传不解析,前端可选消费。
# emit_platform_status 接受 **extra 转发白名单字段到 payload。
# 当前白名单:_EXTRA_FIELDS(模块级 frozenset)
# ---------------------------------------------------------------------------


def test_emit_accepts_source_url_via_extra(capsys):
    """``source_url`` 加性字段通过 **extra 透传到 payload。"""
    emit_platform_status(
        "bilibili_BV1xx",
        "bilibili",
        "uploading",
        source_url="https://www.bilibili.com/video/BV1xx",
    )
    captured = capsys.readouterr()
    line = [l for l in captured.out.splitlines() if l.startswith("__EVENT__:")][0]
    payload = json.loads(line[len("__EVENT__:") :])
    assert payload["source_url"] == "https://www.bilibili.com/video/BV1xx"
    assert payload["platform"] == "bilibili"
    assert payload["status"] == "uploading"


def test_emit_accepts_progress_via_extra(capsys):
    """``progress`` 加性字段(0-100 浮点)通过 **extra 透传。"""
    emit_platform_status(
        "bilibili_BV1xx",
        "bilibili",
        "uploading",
        progress=42.5,
    )
    captured = capsys.readouterr()
    line = [l for l in captured.out.splitlines() if l.startswith("__EVENT__:")][0]
    payload = json.loads(line[len("__EVENT__:") :])
    assert payload["progress"] == 42.5


def test_emit_combines_source_url_and_progress(capsys):
    """同时传 source_url + progress → 两个字段都在 payload。"""
    emit_platform_status(
        "bilibili_BV1xx",
        "bilibili",
        "uploading",
        source_url="https://www.bilibili.com/video/BV1xx",
        progress=100.0,
    )
    captured = capsys.readouterr()
    line = [l for l in captured.out.splitlines() if l.startswith("__EVENT__:")][0]
    payload = json.loads(line[len("__EVENT__:") :])
    assert payload["source_url"] == "https://www.bilibili.com/video/BV1xx"
    assert payload["progress"] == 100.0


def test_emit_omits_extra_fields_when_none(capsys):
    """显式传 None 的加性字段不应出现在 payload(避免 ``source_url: null`` 噪音)。"""
    emit_platform_status(
        "article.md",
        "toutiao",
        "uploading",
        source_url=None,
        progress=None,
    )
    captured = capsys.readouterr()
    line = [l for l in captured.out.splitlines() if l.startswith("__EVENT__:")][0]
    payload = json.loads(line[len("__EVENT__:") :])
    assert "source_url" not in payload
    assert "progress" not in payload


def test_emit_ignores_unknown_extra_keys(capsys):
    """**extra 里的非白名单 key 静默忽略(防御边界 + 未来加新字段不破签名)。"""
    emit_platform_status(
        "article.md",
        "toutiao",
        "uploading",
        source_url="https://example.com",
        unknown_field="should be dropped",  # 不在 _EXTRA_FIELDS
        another_random_key=42,
    )
    captured = capsys.readouterr()
    line = [l for l in captured.out.splitlines() if l.startswith("__EVENT__:")][0]
    payload = json.loads(line[len("__EVENT__:") :])
    assert "unknown_field" not in payload
    assert "another_random_key" not in payload
    assert payload["source_url"] == "https://example.com"


def test_emit_extra_fields_combine_with_url_error(capsys):
    """加性字段与 url / error 共存(典型 success / failed 终态)。"""
    # success
    emit_platform_status(
        "bilibili_BV1xx",
        "bilibili",
        "success",
        url="https://b.oss.aliyuncs.com/k.mp4",
        source_url="https://www.bilibili.com/video/BV1xx",
    )
    line = [l for l in capsys.readouterr().out.splitlines() if l.startswith("__EVENT__:")][0]
    p = json.loads(line[len("__EVENT__:") :])
    assert p["url"] == "https://b.oss.aliyuncs.com/k.mp4"
    assert p["source_url"] == "https://www.bilibili.com/video/BV1xx"
    assert p["status"] == "success"


def test_emit_for_bilibili_uses_valid_platforms(capsys):
    """emit bilibili 不抛 ValueError(从 VALID_PLATFORMS 白名单放行)。"""
    emit_platform_status("bilibili_BV1xx", "bilibili", "logging_in")
    line = [l for l in capsys.readouterr().out.splitlines() if l.startswith("__EVENT__:")][0]
    p = json.loads(line[len("__EVENT__:") :])
    assert p["platform"] == "bilibili"


def test_emit_passes_url_error_screenshot(capsys):
    """url/error/screenshot kwargs 正确传递到 payload。"""
    emit_platform_status(
        "article.md",
        "toutiao",
        "failed",
        url="https://example.com/draft",
        error="element not found: .editor",
        screenshot="C:\\shots\\article.png",
    )
    captured = capsys.readouterr()
    line = [l for l in captured.out.splitlines() if l.startswith("__EVENT__:")][0]
    payload = json.loads(line[len("__EVENT__:") :])

    assert payload["url"] == "https://example.com/draft"
    assert payload["error"] == "element not found: .editor"
    assert payload["screenshot"] == "C:\\shots\\article.png"


def test_emit_flushes_immediately(capsys):
    """flush=True 关键:Tauri 端 BufReader::lines() 看不到未 flush 的 buffer。

    pytest capsys 默认捕获所有 stdout,这里通过验证 output 非空间接验证 flush 有效。
    (实际 flush 行为在 Python 进程层,测试只能验证 stdout 有内容。)
    """
    emit_platform_status("article.md", "toutiao", "logging_in")
    captured = capsys.readouterr()
    assert "__EVENT__:" in captured.out


# ---------------------------------------------------------------------------
# ts 字段格式测试(SPEC §3.5 ISO 8601 UTC + 毫秒精度 + Z 后缀)
# ---------------------------------------------------------------------------


def test_now_iso_format():
    """now_iso() 返回格式:2026-06-22T17:00:00.123Z(26 字符)。"""
    ts = now_iso()
    # ISO 8601 UTC: YYYY-MM-DDTHH:MM:SS.mmmZ(26 chars)
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$", ts
    ), f"unexpected ts format: {ts!r}"


def test_now_iso_changes_over_time():
    """两次调用 now_iso() 应该有微小时间差(毫秒精度可观察到)。"""
    t1 = now_iso()
    t2 = now_iso()
    # t2 应该 >= t1 (字符串可比,因为 ISO 8601 字典序 == 时间序)
    assert t2 >= t1


def test_emit_ts_matches_now_iso_format(capsys):
    """emit_platform_status 输出的 ts 字段格式正确。"""
    emit_platform_status("article.md", "toutiao", "uploading")
    captured = capsys.readouterr()
    line = [l for l in captured.out.splitlines() if l.startswith("__EVENT__:")][0]
    payload = json.loads(line[len("__EVENT__:") :])

    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$",
        payload["ts"],
    ), f"unexpected ts in payload: {payload['ts']!r}"


# ---------------------------------------------------------------------------
# Phase 30a Spec 5:entry_id threading
# ---------------------------------------------------------------------------
#
# 多 Entry 并行场景:Rust 端 cmd.env("AUTO_PUBLISH_ENTRY_ID", entry_id) 注入,
# Python 端 event_emitter 读 env + 在每个 __EVENT__:{...} payload include entry_id。
# 前端 applyEvent 按 evt.entryId 路由到 cellsByEntry[id]。
#
# 测试覆盖:
#   - 默认(None env):emit 时 entry_id=null(spec 3 行为)
#   - set_current_entry_id 后:下次 emit 用新值
#   - get_current_entry_id 返回当前状态


def test_entry_id_null_when_env_not_set(capsys):
    """未设置 env var(且未调 set_current_entry_id)时,emit entry_id=null。"""
    # 防御:重置 module-level 状态(其他 test 可能 set 过)
    set_current_entry_id(None)
    emit_platform_status("a.md", "toutiao", "success")
    captured = capsys.readouterr()
    line = [l for l in captured.out.splitlines() if l.startswith("__EVENT__:")][0]
    payload = json.loads(line[len("__EVENT__:") :])
    assert payload["entry_id"] is None
    # cleanup:重置回 None,不影响其他 test
    set_current_entry_id(None)


def test_set_current_entry_id_overrides(capsys):
    """set_current_entry_id 后,emit 用新值(env var 覆盖顺序:CLI > env > None)。"""
    set_current_entry_id("entry_alpha")
    emit_platform_status("a.md", "toutiao", "uploading")
    captured = capsys.readouterr()
    line = [l for l in captured.out.splitlines() if l.startswith("__EVENT__:")][0]
    payload = json.loads(line[len("__EVENT__:") :])
    assert payload["entry_id"] == "entry_alpha"
    # 切回 None(后续 test 不受污染)
    set_current_entry_id(None)
    emit_platform_status("b.md", "toutiao", "uploading")
    captured = capsys.readouterr()
    line = [l for l in captured.out.splitlines() if l.startswith("__EVENT__:")][0]
    payload = json.loads(line[len("__EVENT__:") :])
    assert payload["entry_id"] is None


def test_get_current_entry_id_returns_state():
    """get_current_entry_id 返回 module-level 状态(供 retry_engine / cancellation 共享)。"""
    set_current_entry_id("entry_xyz")
    assert get_current_entry_id() == "entry_xyz"
    set_current_entry_id(None)
    assert get_current_entry_id() is None


# ---------------------------------------------------------------------------
# Phase 48 Spec 6:make_event_cb entry_id threading(补 Phase 30a 漏的 emit 入口)
# ---------------------------------------------------------------------------
#
# Phase 30a Spec 5 实现了 `event_emitter.emit_platform_status` payload include entry_id,
# 但漏了 `publisher.__init__.make_event_cb`(spec 3 时代 `__EVENT__:` 行包装,
# 被 run_dispatch / publish_xxx 多处用)。Phase 48 补 make_event_cb cb
# 函数 inject entry_id,让 spec 6 dispatch 模式走同一条 entry_id 路径。
#
# 测试覆盖(3 个):
#   - inject module-level entry_id(set_current_entry_id 后 cb 写出 entry_id)
#   - caller 显式传 entry_id 时不覆盖(允许 spec 6 dispatch_completed 顶层 override)
#   - module-level None → payload entry_id = None(JSON null,spec 3 路径)
# ---------------------------------------------------------------------------


def test_make_event_cb_injects_entry_id(capsys):
    """Phase 48:make_event_cb cb 函数 inject entry_id 字段。

    set_current_entry_id(eid) 后 cb(payload) 写出 __EVENT__:JSON 含 payload.entry_id=eid,
    让前端 applyEvent 按 evt.entryId 路由到 cellsByEntry[id]。
    """
    from multimedia_parsing.event_emitter import set_current_entry_id
    from multimedia_parsing.event_emitter import make_event_cb

    set_current_entry_id("entry_thread")  # Phase 48 spec 6 场景
    try:
        cb = make_event_cb()
        cb({"type": "custom_event", "foo": "bar"})
        captured = capsys.readouterr()
        lines = [l for l in captured.out.splitlines() if l.startswith("__EVENT__:")]
        assert len(lines) == 1
        payload = json.loads(lines[0][len("__EVENT__:") :])
        assert payload["type"] == "custom_event"
        assert payload["foo"] == "bar"
        assert payload["entry_id"] == "entry_thread"
    finally:
        set_current_entry_id(None)


def test_make_event_cb_caller_explicit_entry_id_wins(capsys):
    """caller 显式传 entry_id 时不覆盖(允许 spec 6 顶层事件 override)。

    Phase 48 场景:dispatch_completed 等顶层事件如果需要标"整 entry 完成"
    语义,允许显式 override module-level 状态。
    """
    from multimedia_parsing.event_emitter import set_current_entry_id
    from multimedia_parsing.event_emitter import make_event_cb

    set_current_entry_id("entry_default")
    try:
        cb = make_event_cb()
        cb({"type": "override", "entry_id": "entry_override"})
        captured = capsys.readouterr()
        lines = [l for l in captured.out.splitlines() if l.startswith("__EVENT__:")]
        assert len(lines) == 1
        payload = json.loads(lines[0][len("__EVENT__:") :])
        # caller 显式传的值胜出,不读 module-level
        assert payload["entry_id"] == "entry_override"
    finally:
        set_current_entry_id(None)


def test_make_event_cb_entry_id_null_when_not_set(capsys):
    """module-level entry_id=None → payload entry_id=None(spec 3 / 单跑路径)。

    跟 Phase 30a test_entry_id_null_when_env_not_set 同款 spec 3 行为,
    验证 Phase 48 cb 注入跟 emit_platform_status 一致(spec 3 路径 null)。
    """
    from multimedia_parsing.event_emitter import set_current_entry_id
    from multimedia_parsing.event_emitter import make_event_cb

    set_current_entry_id(None)
    cb = make_event_cb()
    cb({"type": "no_entry_spec3"})
    captured = capsys.readouterr()
    lines = [l for l in captured.out.splitlines() if l.startswith("__EVENT__:")]
    assert len(lines) == 1
    payload = json.loads(lines[0][len("__EVENT__:") :])
    assert payload["entry_id"] is None


# ---------------------------------------------------------------------------
# 总结性测试
# ---------------------------------------------------------------------------


def test_summary_all_9_statuses_round_trip_through_json(capsys):
    """9 个状态都序列化 → 反序列化 → 字段无丢失。"""
    for status in sorted(VALID_STATUSES):
        capsys.readouterr()  # 清空 captured
        emit_platform_status(
            "test.md",
            "csdn",
            status,
            url="https://example.com/csdn/test" if status == "success" else None,
            error="模拟失败" if status == "failed" else None,
            screenshot="C:\\shots\\test.png" if status == "failed" else None,
        )
        captured = capsys.readouterr()
        line = [l for l in captured.out.splitlines() if l.startswith("__EVENT__:")][0]
        payload = json.loads(line[len("__EVENT__:") :])
        assert payload["status"] == status
        assert payload["article"] == "test.md"
        assert payload["platform"] == "csdn"


# ---------------------------------------------------------------------------
# Plan 2026-09-02-image-fetch-oss (2026-09-02):新增 emit_image_fetch_status
# ---------------------------------------------------------------------------
# 权威定义:docs/superpowers/specs/2026-09-02-image-fetch-oss-design.md §4.3 + F3.1。
# 解析阶段回传"图片列表"装不下 platform_status 单条 payload,故另立 type。
# ---------------------------------------------------------------------------


def test_emit_image_fetch_status_emits_event_line(capsys):
    """emit_image_fetch_status 往 stdout 打 __EVENT__: JSON 行(type=image_fetch_status)。"""
    from multimedia_parsing.event_emitter import emit_image_fetch_status  # 新加,本文件内首次引用

    emit_image_fetch_status(
        batch_id="img-001",
        phase="parse",
        status="success",
        source_url="https://example.com",
        images=[{"url": "https://cdn/a.jpg"}],
    )
    captured = capsys.readouterr()
    line = [l for l in captured.out.splitlines() if l.startswith("__EVENT__:")][-1]
    payload = json.loads(line[len("__EVENT__:"):])
    assert payload["type"] == "image_fetch_status"
    assert payload["batch_id"] == "img-001"
    assert payload["phase"] == "parse"
    assert payload["status"] == "success"
    assert payload["source_url"] == "https://example.com"
    assert payload["images"] == [{"url": "https://cdn/a.jpg"}]
    assert "ts" in payload


def test_emit_image_fetch_status_progress_dict_shape(capsys):
    """progress 字段是 {done, total} dict(区别于 video 模块的 float 0-100,spec §4.3)。"""
    from multimedia_parsing.event_emitter import emit_image_fetch_status

    emit_image_fetch_status(
        batch_id="img-001",
        phase="download",
        status="processing",
        image_url="https://cdn/a.jpg",
        progress={"done": 3, "total": 12},
    )
    captured = capsys.readouterr()
    line = [l for l in captured.out.splitlines() if l.startswith("__EVENT__:")][-1]
    payload = json.loads(line[len("__EVENT__:"):])
    assert payload["progress"] == {"done": 3, "total": 12}


def test_emit_image_fetch_status_none_fields_omitted(capsys):
    """加性字段 None 不发(防御 payload 漂移)。"""
    from multimedia_parsing.event_emitter import emit_image_fetch_status

    emit_image_fetch_status(
        batch_id="img-001",
        phase="download",
        status="success",
        url="file:///D:/pics/01_a.jpg",
    )
    captured = capsys.readouterr()
    line = [l for l in captured.out.splitlines() if l.startswith("__EVENT__:")][-1]
    payload = json.loads(line[len("__EVENT__:"):])
    assert "source_url" not in payload
    assert "image_url" not in payload
    assert "images" not in payload
    assert "progress" not in payload
    assert "error" not in payload
    assert payload["url"] == "file:///D:/pics/01_a.jpg"


def test_emit_image_fetch_status_rejects_empty_batch_id():
    from multimedia_parsing.event_emitter import emit_image_fetch_status

    with pytest.raises(ValueError, match="batch_id must be non-empty"):
        emit_image_fetch_status(batch_id="", phase="parse", status="success")


def test_emit_image_fetch_status_rejects_invalid_phase():
    from multimedia_parsing.event_emitter import emit_image_fetch_status

    with pytest.raises(ValueError, match="phase must be one of"):
        emit_image_fetch_status(
            batch_id="img-001", phase="convert", status="success"
        )


def test_emit_image_fetch_status_rejects_invalid_status():
    from multimedia_parsing.event_emitter import emit_image_fetch_status

    with pytest.raises(ValueError, match="unknown status"):
        emit_image_fetch_status(
            batch_id="img-001", phase="parse", status="bogus"
        )


def test_valid_image_phases_includes_required_set():
    """三阶段 parse / download / upload(spec §4.3 F3.1 决策)。"""
    from multimedia_parsing.event_emitter import VALID_IMAGE_PHASES

    assert "parse" in VALID_IMAGE_PHASES
    assert "download" in VALID_IMAGE_PHASES
    assert "upload" in VALID_IMAGE_PHASES
    assert len(VALID_IMAGE_PHASES) == 3





