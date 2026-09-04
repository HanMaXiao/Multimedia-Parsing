"""test_video_fetcher_storage_state_adapter.py — storage_state_adapter 单元测试。

覆盖范围:
  - 正常 storage_state → Netscape 文本格式正确(7 字段 tab 分隔 + header)
  - 子域 flag(以 . 开头的 domain → TRUE)
  - httpOnly 前缀(#HttpOnly_)
  - session cookie(expires=-1 → 0)
  - 缺 cookies 字段 → StorageStateError
  - cookies 不是 list → StorageStateError
  - 空 cookies list → None(降级无登录)
  - 坏 JSON / 不是 dict → 抛错
  - 单条 cookie 缺关键字段 → 整批 reject
  - I/O:写临时文件成功 + 路径有效
  - 缺失 storage_state 文件 → FileNotFoundError
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from multimedia_parsing.video_fetcher.storage_state_adapter import (
    COOKIEFILE_SUFFIX,
    StorageStateError,
    build_cookiefile_content,
    to_netscape_cookiefile,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _bilibili_cookie(**overrides) -> dict:
    """单条典型 B 站登录态 cookie(DedeUserID)。"""
    base = {
        "name": "DedeUserID",
        "value": "123456789",
        "domain": ".bilibili.com",
        "path": "/",
        "expires": 1893456000,  # 2030-01-01 UTC
        "httpOnly": False,
        "secure": True,
        "sameSite": "Lax",
    }
    base.update(overrides)
    return base


def _sample_state() -> dict:
    return {
        "cookies": [
            _bilibili_cookie(),
            _bilibili_cookie(
                name="SESSDATA",
                value="abc%2Cdef",
                httpOnly=True,
                path="/",
            ),
            _bilibili_cookie(
                name="session_expiry",
                value="0",
                expires=-1,  # session cookie
                httpOnly=False,
            ),
        ],
        "origins": [],
    }


# ---------------------------------------------------------------------------
# build_cookiefile_content — 正常路径
# ---------------------------------------------------------------------------


def test_build_returns_netscape_header():
    out = build_cookiefile_content(_sample_state())
    assert out is not None
    assert out.startswith("# Netscape HTTP Cookie File\n")
    assert "# This is a generated file! Do not edit." in out


def test_build_separates_httpOnly_into_prefixed_section():
    """httpOnly cookies 进 #HttpOnly_ 段,普通 cookie 进 main 段。"""
    out = build_cookiefile_content(_sample_state())
    assert out is not None
    lines = out.splitlines()
    # main 区第一行是 header
    assert lines[0] == "# Netscape HTTP Cookie File"
    # DedeUserID(httpOnly=False)在 main 区
    assert any(
        l.startswith(".bilibili.com\t") and "DedeUserID\t" in l
        for l in lines
    )
    # SESSDATA(httpOnly=True)有 #HttpOnly_ 前缀
    assert any(
        l.startswith("#HttpOnly_.bilibili.com\t") and "SESSDATA\t" in l
        for l in lines
    )


def test_build_subdomain_flag_true_for_leading_dot():
    """``domain=.bilibili.com`` → flag=TRUE(子域共享)。"""
    state = {"cookies": [_bilibili_cookie(domain=".bilibili.com")], "origins": []}
    out = build_cookiefile_content(state)
    assert out is not None
    assert "\tTRUE\t" in out


def test_build_subdomain_flag_false_for_exact_host():
    """``domain=www.bilibili.com``(无前导 .)→ flag=FALSE(仅该 host)。"""
    state = {
        "cookies": [_bilibili_cookie(domain="www.bilibili.com")],
        "origins": [],
    }
    out = build_cookiefile_content(state)
    assert out is not None
    # 域名前导 . 是 .bilibili.com,exact host 走 FALSE
    line = [l for l in out.splitlines() if "DedeUserID" in l][0]
    fields = line.split("\t")
    assert fields[0] == "www.bilibili.com"
    assert fields[1] == "FALSE"


def test_build_session_cookie_expires_zero():
    """Playwright expires=-1 → Netscape 0(session cookie)。"""
    state = {
        "cookies": [_bilibili_cookie(expires=-1)],
        "origins": [],
    }
    out = build_cookiefile_content(state)
    assert out is not None
    line = [l for l in out.splitlines() if "DedeUserID" in l][0]
    fields = line.split("\t")
    assert fields[4] == "0"


def test_build_explicit_expiration_preserved():
    """正向 expires 时间戳原样写入。"""
    state = {
        "cookies": [_bilibili_cookie(expires=1893456000)],
        "origins": [],
    }
    out = build_cookiefile_content(state)
    assert out is not None
    line = [l for l in out.splitlines() if "DedeUserID" in l][0]
    fields = line.split("\t")
    assert fields[4] == "1893456000"


def test_build_secure_true_or_false():
    """secure=TRUE / FALSE 区分。"""
    state_a = {"cookies": [_bilibili_cookie(secure=True)], "origins": []}
    state_b = {"cookies": [_bilibili_cookie(secure=False)], "origins": []}
    a = build_cookiefile_content(state_a)
    b = build_cookiefile_content(state_b)
    assert a is not None and b is not None
    assert "\tTRUE\t" in a
    assert "\tFALSE\t" in b


def test_build_value_with_special_chars_preserved():
    """value 含 % / , / + 等特殊字符 — Netscape 规范用字面,不做转义。"""
    state = {
        "cookies": [_bilibili_cookie(name="SESSDATA", value="abc%2Cdef,ghi+jkl")],
        "origins": [],
    }
    out = build_cookiefile_content(state)
    assert out is not None
    # 整行内容包含原文 value(无转义)
    assert "abc%2Cdef,ghi+jkl" in out


# ---------------------------------------------------------------------------
# build_cookiefile_content — 降级 / 边界
# ---------------------------------------------------------------------------


def test_build_returns_none_for_empty_cookies_list():
    """空 cookies list → None(降级无登录,允许 Phase 3 run_fetch 走 ≤480p)。"""
    state = {"cookies": [], "origins": []}
    assert build_cookiefile_content(state) is None


def test_build_returns_none_for_empty_cookies_with_origins():
    """cookies=空 list 但 origins 非空 → 仍 None(yt-dlp 不用 origins)。"""
    state = {"cookies": [], "origins": [{"origin": "https://www.bilibili.com"}]}
    assert build_cookiefile_content(state) is None


def test_build_raises_on_missing_cookies_field():
    """缺 cookies 字段(注意是 None,不是 [])→ StorageStateError,跟空 list 区分。"""
    state = {"origins": []}
    with pytest.raises(StorageStateError, match="missing 'cookies' field"):
        build_cookiefile_content(state)


def test_build_raises_on_cookies_not_list():
    state = {"cookies": "oops"}
    with pytest.raises(StorageStateError, match="'cookies' must be a list"):
        build_cookiefile_content(state)


def test_build_raises_on_non_dict_state():
    with pytest.raises(StorageStateError, match="must be a JSON object"):
        build_cookiefile_content([])  # type: ignore[arg-type]


def test_build_raises_on_cookie_missing_name():
    bad = {"cookies": [{"value": "v", "domain": ".x.com", "path": "/"}]}
    with pytest.raises(StorageStateError, match="missing or invalid 'name'"):
        build_cookiefile_content(bad)


def test_build_raises_on_cookie_missing_domain():
    bad = {"cookies": [{"name": "k", "value": "v", "path": "/"}]}
    with pytest.raises(StorageStateError, match="missing or invalid 'domain'"):
        build_cookiefile_content(bad)


def test_build_raises_on_cookie_missing_path():
    bad = {"cookies": [{"name": "k", "value": "v", "domain": ".x.com"}]}
    with pytest.raises(StorageStateError, match="missing or invalid 'path'"):
        build_cookiefile_content(bad)


# ---------------------------------------------------------------------------
# to_netscape_cookiefile — I/O
# ---------------------------------------------------------------------------


def test_to_cookiefile_writes_tmp_file(tmp_path: Path):
    state_file = tmp_path / "storage_bilibili.json"
    state_file.write_text(json.dumps(_sample_state()), encoding="utf-8")

    cookie_path = to_netscape_cookiefile(state_file)

    assert cookie_path is not None
    assert cookie_path.exists()
    assert cookie_path.suffix == COOKIEFILE_SUFFIX
    # 写出的内容能反向解析回 cookie(至少含 DedeUserID 行)
    content = cookie_path.read_text(encoding="utf-8")
    assert "DedeUserID" in content
    assert "# Netscape HTTP Cookie File" in content


def test_to_cookiefile_returns_none_for_empty_cookies(tmp_path: Path):
    state_file = tmp_path / "empty.json"
    state_file.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    assert to_netscape_cookiefile(state_file) is None


def test_to_cookiefile_raises_on_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        to_netscape_cookiefile(tmp_path / "does_not_exist.json")


def test_to_cookiefile_raises_on_bad_json(tmp_path: Path):
    state_file = tmp_path / "bad.json"
    state_file.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        to_netscape_cookiefile(state_file)


def test_to_cookiefile_raises_on_invalid_state_structure(tmp_path: Path):
    """cookies 不是 list → 写文件前已 raise,不应该留临时文件。"""
    state_file = tmp_path / "bad_struct.json"
    state_file.write_text(json.dumps({"cookies": "oops"}), encoding="utf-8")
    with pytest.raises(StorageStateError):
        to_netscape_cookiefile(state_file)


def test_to_cookiefile_silent_corrupted_cookie_rejects_whole_batch(tmp_path: Path):
    """单条 cookie 缺关键字段 → 整批 reject(不留半残文件)。"""
    state = {
        "cookies": [
            _bilibili_cookie(),
            {"name": "broken", "value": "v"},  # 缺 domain / path
        ],
        "origins": [],
    }
    state_file = tmp_path / "partial_bad.json"
    state_file.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(StorageStateError):
        to_netscape_cookiefile(state_file)

