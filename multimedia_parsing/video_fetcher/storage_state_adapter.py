"""video_fetcher.storage_state_adapter — Playwright storage_state → Netscape cookies.txt。

权威定义:spec §7 B 站登录态复用决策(D4);adapter 把项目 ``accounts/{user_id}/storage_bilibili.json``
(Playwright storage_state 标准格式)转成 yt-dlp 可吃的 Netscape cookies.txt 临时文件。

设计要点:
  - 纯函数 ``build_cookiefile_content(state) -> Optional[str]``(无 I/O,便于单测)。
  - I/O 包装 ``to_netscape_cookiefile(state_path) -> Optional[Path]``(写临时文件)。
  - 降级策略(storage_state 缺失 / 坏 JSON / cookies 为空)→ 返 None,调用方走无登录态(≤480p)。
  - 临时文件落 ``runs/{batch_id}/cookies.txt``(已 gitignore),用后由 Phase 7 runs 清理机制兜底。
  - 不主动清理临时文件:yt-dlp ``cookiefile`` 读取是打开-读取-关闭,文件句柄释放后进程仍可读。

被拒(2026-09-02 用户拍板):
  - 设置面板单独配「解析专用 Cookie」 — 拒绝:双份 B 站登录态,失效排查混乱。
  - yt-dlp ``--cookies-from-browser`` — 拒绝:依赖用户本机浏览器 profile,与项目账号体系脱节。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# Netscape cookies.txt 7 字段(tab 分隔):
#   domain  flag  path  secure  expiration  name  value
#
# Playwright storage_state cookie 字段 → Netscape 字段映射:
#   domain        -> domain
#   path          -> path
#   expires       -> expiration(-1 = session cookie,转 0 表示永不过期;Netscape 规范)
#   secure        -> secure(TRUE/FALSE 大写)
#   (无)          -> flag(TRUE=含子域 / FALSE=仅 exact domain,看 domain 是否以 . 开头)
#   name          -> name
#   value         -> value
#   httpOnly      -> 写不进 Netscape(httpOnly 是 HTTP-only 标志,Netscape 不区分;忽略)
#   sameSite      -> Netscape 不支持,忽略

# 临时文件 suffix(.txt 让 yt-dlp 启发式识别;yt-dlp 也支持无后缀,这里给后缀稳一点)
COOKIEFILE_SUFFIX = ".txt"

# session cookie 过期值:Playwright 用 -1,Netscape 规范用 0
_SESSION_COOKIE_EXPIRES = -1
_NETSCAPE_SESSION_EXPIRES = 0


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class StorageStateError(ValueError):
    """storage_state 解析失败(坏 JSON / 不是 dict / 缺 cookies 字段)。"""


# ---------------------------------------------------------------------------
# 纯函数:state dict → Netscape 字符串
# ---------------------------------------------------------------------------


def _flag_for_domain(domain: str) -> str:
    """Netscape flag 字段:domain 以 . 开头 = 子域共享 cookie → TRUE。

    例子:
      ``.bilibili.com`` → TRUE(子域共享)
      ``www.bilibili.com`` → FALSE(仅该 host)
      ``bilibili.com``(无前导 .)→ FALSE(Netscape 规范里需要前导 . 才是 wildcard)
    """
    return "TRUE" if domain.startswith(".") else "FALSE"


def _format_expiration(expires: Any) -> str:
    """Playwright expires → Netscape expiration。

    -1 / None / 0 → session cookie → 0(Netscape 规范)
    正数 → 原值(已经是 Unix 秒)
    """
    if expires is None:
        return str(_NETSCAPE_SESSION_EXPIRES)
    if not isinstance(expires, (int, float)):
        return str(_NETSCAPE_SESSION_EXPIRES)
    if expires < 0:
        return str(_NETSCAPE_SESSION_EXPIRES)
    return str(int(expires))


def _format_cookie_line(cookie: Dict[str, Any]) -> str:
    """单条 Playwright cookie → 一行 Netscape 文本。

    缺关键字段(name / value / domain / path)→ 抛 ``StorageStateError``,调用方
    整体 reject(单条坏 cookie 不能静默跳过,免得 yt-dlp 拿半残 cookie 出现偶发失败)。
    """
    name = cookie.get("name")
    value = cookie.get("value", "")
    domain = cookie.get("domain")
    path = cookie.get("path")
    if not isinstance(name, str) or not name:
        raise StorageStateError(
            f"storage_state cookie missing or invalid 'name': {cookie!r}"
        )
    if not isinstance(domain, str) or not domain:
        raise StorageStateError(
            f"storage_state cookie {name!r} missing or invalid 'domain': {cookie!r}"
        )
    if not isinstance(path, str) or not path:
        raise StorageStateError(
            f"storage_state cookie {name!r} missing or invalid 'path': {cookie!r}"
        )

    flag = _flag_for_domain(domain)
    secure = "TRUE" if cookie.get("secure") else "FALSE"
    expiration = _format_expiration(cookie.get("expires"))
    # value 可能含 tab / 换行 / 特殊字符 — 不做转义,Netscape 规范用字面,
    # yt-dlp / curl 都按行解析,空 value 合法。
    return f"{domain}\t{flag}\t{path}\t{secure}\t{expiration}\t{name}\t{value}"


# Netscape 文件头(header 注释行,yt-dlp 启发式依赖 #HttpOnly_ 标记)
_HEADER_LINES = [
    "# Netscape HTTP Cookie File",
    "# https://curl.haxx.se/rfc/cookie_spec.html",
    "# This is a generated file! Do not edit.",
]


def build_cookiefile_content(state: Dict[str, Any]) -> Optional[str]:
    """从 Playwright storage_state dict 生成 Netscape cookies.txt 文本。

    Parameters
    ----------
    state:
        Playwright storage_state 格式,``{"cookies": [...], "origins": [...]}``。

    Returns
    -------
    Optional[str]:
        完整的 Netscape cookies.txt 文本(含 header)。
        ``cookies`` 列表为空 / ``state`` 缺 ``cookies`` 字段 → 返 ``None``(降级无登录态)。

    Raises
    ------
    StorageStateError:
        ``state`` 不是 dict / ``cookies`` 不是 list / 单条 cookie 缺关键字段。
    """
    if not isinstance(state, dict):
        raise StorageStateError(
            f"storage_state must be a JSON object, got {type(state).__name__}"
        )
    cookies = state.get("cookies")
    if cookies is None:
        # 缺字段(而不是空 list) — 这是坏 storage_state,跟 cookies=[] 区分
        raise StorageStateError("storage_state missing 'cookies' field")
    if not isinstance(cookies, list):
        raise StorageStateError(
            f"storage_state 'cookies' must be a list, got {type(cookies).__name__}"
        )
    if not cookies:
        # 空 list 是合法 Playwright 输出(用户登出后),但对 yt-dlp 无意义 → 降级
        return None

    # httpOnly cookie 要前缀 ``#HttpOnly_``(Netscape 扩展,yt-dlp 支持)。
    # 简单实现:每条都判定一次,分别进 main 区 / HttpOnly 区。
    main_lines: List[str] = list(_HEADER_LINES)
    httponly_lines: List[str] = list(_HEADER_LINES)
    for cookie in cookies:
        line = _format_cookie_line(cookie)
        if cookie.get("httpOnly"):
            httponly_lines.append("#HttpOnly_" + line)
        else:
            main_lines.append(line)

    # 两区都用相同 header(Netscape 规范:header 注释行重复无害)
    if httponly_lines == _HEADER_LINES:
        # 没有 httpOnly 行 — 只输出 main
        return "\n".join(main_lines) + "\n"
    if main_lines == _HEADER_LINES:
        # 全部是 httpOnly — 只输出 HttpOnly 区
        return "\n".join(httponly_lines) + "\n"
    # 两区都非空 — main 在前,HttpOnly 在后
    return "\n".join(main_lines) + "\n" + "\n".join(httponly_lines) + "\n"


# ---------------------------------------------------------------------------
# I/O 包装:state 文件 → 临时 cookiefile 路径
# ---------------------------------------------------------------------------


def to_netscape_cookiefile(state_path: Path) -> Optional[Path]:
    """从 storage_state JSON 文件生成临时 Netscape cookies.txt。

    Parameters
    ----------
    state_path:
        Playwright storage_state JSON 路径(项目约定
        ``accounts/{user_id}/storage_{platform}.json``)。

    Returns
    -------
    Optional[Path]:
        生成的临时文件绝对路径。调用方传给 yt-dlp ``cookiefile`` 参数。
        ``None`` = 无可用 cookie(走无登录态降级,≤480p)。

    Raises
    ------
    FileNotFoundError:
        state_path 不存在(由 ``Path.read_text`` 抛出,语义清晰)。
    json.JSONDecodeError:
        state_path 不是合法 JSON。
    StorageStateError:
        state 结构坏(非 dict / cookies 不是 list / 单条 cookie 缺关键字段)。
    OSError:
        写临时文件失败(磁盘满 / 权限)。
    """
    p = Path(state_path)
    raw = p.read_text(encoding="utf-8")
    state = json.loads(raw)
    content = build_cookiefile_content(state)
    if content is None:
        return None

    # 临时文件落 runs/{batch_id}/cookies.txt(由调用方传入 dir,或 fallback 系统 tmp)。
    # 这里用系统 tmp 是因为:
    #   1. storage_state_adapter 不该耦合 manifest 字段(单一职责);
    #   2. yt-dlp cookiefile 不在意位置(只要能读);
    #   3. 上层 run_fetch 可以拿到路径后 mv 到 runs/{batch_id}/(已 gitignore)。
    # 用 ``NamedTemporaryFile(delete=False)`` 而非 ``mkstemp`` —— 后者要手 close fd,
    # Windows 上 fd 持有期间再 open 同一文件会冲突(yt-dlp 也要开)。
    fd, name = tempfile.mkstemp(suffix=COOKIEFILE_SUFFIX, prefix="vf_cookies_")
    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        # 写失败清理孤儿临时文件,避免污染 tmp
        try:
            Path(name).unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return Path(name)


__all__ = [
    "COOKIEFILE_SUFFIX",
    "StorageStateError",
    "build_cookiefile_content",
    "to_netscape_cookiefile",
]

