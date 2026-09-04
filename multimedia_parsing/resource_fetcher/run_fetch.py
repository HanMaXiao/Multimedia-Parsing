"""resource_fetcher.run_fetch — 下载/上传阶段编排 (Phase 1 Commit 3).

权威定义: docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md §4 + §5.1 + §7 + §9.

设计要点:
  - 逐 item 调对应 resolver (按 resource_type 选).
  - 事件发射 (run_fetch 通过注入的 event_cb).
  - 单 item 失败不中断整批 (spec §8 部分成功).
  - both 模式 OSS 失败降级 (spec §9): 整体 success + result.oss_error + message 提示.
  - 整批可取消 (spec §8 验收 8): threading.Event set 后,后续 item 不入 results.

测试用 monkeypatch.setattr(_select_resolver_for_resource_type) 替换路由逻辑.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional

from .base import FetchDestination, FetchResult
from .manifest import ResourceFetchItem, ResourceFetchManifest
from .resolvers.image import ImageResolver
from .resolvers.video import VideoResolver


# ---------------------------------------------------------------------------
# 路由选择 (test-exposed, monkeypatch 入口)
# ---------------------------------------------------------------------------


def _select_resolver_for_resource_type(
    resource_type: str,
    *,
    cookie_file=None,
    account_id=None,
) -> Optional[Any]:
    """按 item.resource_type 选 resolver instance.

    返回带 batch-level context (cookie_file / account_id) 的 resolver. cookie_file 来自
    ResourceFetchManifest.cookie_file (video auth); image 类不需要.

    Returns:
        Resolver instance (VideoResolver / ImageResolver) 或 None (audio / model D2 预留).
    """
    return _make_resolver(resource_type, cookie_file=cookie_file, account_id=account_id)


def _make_resolver(resource_type: str, *, cookie_file=None, account_id=None) -> Optional[Any]:
    if resource_type == "video":
        return VideoResolver(cookie_file=cookie_file, account_id=account_id)
    if resource_type == "image":
        return ImageResolver()
    # audio / model D2 预留 — 路由枚举有, resolver 暂无
    return None


# ---------------------------------------------------------------------------
# 事件发射 helper
# ---------------------------------------------------------------------------


EventCallback = Callable[[Dict[str, Any]], None]


def _emit(event_cb: Optional[EventCallback], payload: Dict[str, Any]) -> None:
    if event_cb is None:
        return
    event_cb(payload)


def _build_result_payload(result: FetchResult) -> Dict[str, Any]:
    """把 FetchResult 转成事件 payload.result 字段 (None 字段不发)."""
    out: Dict[str, Any] = {}
    if result.local_path is not None:
        out["local_path"] = str(result.local_path)
    if result.oss_url is not None:
        out["oss_url"] = result.oss_url
    if result.oss_error is not None:
        out["oss_error"] = result.oss_error
    return out


# ---------------------------------------------------------------------------
# 主体
# ---------------------------------------------------------------------------


def run_fetch(
    manifest: ResourceFetchManifest,
    *,
    event_cb: Optional[EventCallback] = None,
    cancel_event: Optional[threading.Event] = None,
    resolver_factory: Optional[Callable[[str], Any]] = None,
) -> List[FetchResult]:
    """逐 item 调对应 resolver → emit download 事件.

    Parameters
    ----------
    manifest:
        已校验的 ResourceFetchManifest (Phase 1 Commit 2 manifest.py 产出).
    event_cb:
        接受 dict payload 的回调. None 时静默. CLI 阶段用 make_event_cb 接 stdout.
    cancel_event:
        threading.Event;set 后当前 item 收尾,后续 item 不入 results. None = 不可取消.
    resolver_factory:
        注入 resolver 选择逻辑. 默认走 _select_resolver_for_resource_type. 测试用
        monkeypatch 替换.

    Returns
    -------
    List[FetchResult]:
        长度 = len(manifest.items);顺序与 items 一致. 每条带 success / failed 终态.
    """
    if resolver_factory is None:
        # 默认 factory: 走 _select_resolver_for_resource_type 并传 manifest cookie_file / account_id
        # (monkeypatch 替换 _select_resolver_for_resource_type 时也能接收这两个参数)
        def _default_factory_with_ctx(resource_type: str):
            return _select_resolver_for_resource_type(resource_type)

        def _default_factory_with_ctx_kt(resource_type: str):
            # 支持新签名 (cookie_file, account_id kwarg) — 默认 _select_resolver_for_resource_type
            return _select_resolver_for_resource_type(
                resource_type,
                cookie_file=manifest.cookie_file,
                account_id=manifest.account_id,
            )

        # monkeypatch 替换 _select_resolver_for_resource_type 为 lambda(rt) 时,
        # _kt 版本会 TypeError. 优先调 _kt,失败回退 _default.
        def _safe_default(resource_type: str):
            try:
                return _default_factory_with_ctx_kt(resource_type)
            except TypeError:
                return _default_factory_with_ctx(resource_type)

        resolver_factory = _safe_default

    def _call_factory(rt: str):
        """兼容新旧 factory 签名 — 老 factory (rt) → 新 factory (rt, *, cookie_file, account_id).

        同事测试用 lambda rt: ... 不接 kwarg,这里 try/except 兜底.
        """
        try:
            return resolver_factory(
                rt,
                cookie_file=manifest.cookie_file,
                account_id=manifest.account_id,
            )
        except TypeError:
            # 旧签名: factory(rt)
            return resolver_factory(rt)

    dest = FetchDestination(
        mode=manifest.mode,
        download_dir=manifest.download_dir,
        oss=manifest.oss,
    )

    results: List[FetchResult] = []
    for item in manifest.items:
        # 1) 整批取消检查
        if cancel_event is not None and cancel_event.is_set():
            break

        # 2) 选 resolver — 按 item.resource_type
        resolver = _call_factory(item.resource_type)
        if resolver is None:
            _emit(
                event_cb,
                {
                    "type": "resource_fetch_status",
                    "batch_id": manifest.batch_id,
                    "stage": "download",
                    "status": "failed",
                    "resource_type": item.resource_type,
                    "item_id": item.item_id,
                    "platform": item.platform,
                    "source_url": item.source_url,
                    "error": (
                        f"unsupported resource_type: {item.resource_type!r} "
                        f"(本期只支持 video / image, audio / model 是 D2 预留)"
                    ),
                },
            )
            results.append(
                FetchResult(
                    item_id=item.item_id,
                    resource_type=item.resource_type,
                    platform=item.platform,
                    error=f"unsupported resource_type: {item.resource_type!r}",
                )
            )
            continue

        # 3) emit processing
        _emit(
            event_cb,
            {
                "type": "resource_fetch_status",
                "batch_id": manifest.batch_id,
                "stage": "download",
                "status": "processing",
                "resource_type": item.resource_type,
                "item_id": item.item_id,
                "platform": item.platform,
                "source_url": item.source_url,
            },
        )

        # 4) 调 resolver.fetch (resolver 内部 try/except; 这里再加一层防御)
        try:
            result = resolver.fetch(item, dest)
        except Exception as e:
            result = FetchResult(
                item_id=item.item_id,
                resource_type=item.resource_type,
                platform=item.platform,
                error=f"fetch: {e}",
            )

        # 5) emit 终态
        if result.is_success:
            result_payload = _build_result_payload(result)
            # both 模式 OSS 失败降级提示 (spec §9)
            message = (
                "本地已保存, OSS 上传失败"
                if result.oss_error is not None
                else None
            )
            _emit(
                event_cb,
                {
                    "type": "resource_fetch_status",
                    "batch_id": manifest.batch_id,
                    "stage": "download",
                    "status": "success",
                    "resource_type": item.resource_type,
                    "item_id": item.item_id,
                    "platform": item.platform,
                    "source_url": item.source_url,
                    "result": result_payload,
                    "message": message,
                },
            )
        else:
            _emit(
                event_cb,
                {
                    "type": "resource_fetch_status",
                    "batch_id": manifest.batch_id,
                    "stage": "download",
                    "status": "failed",
                    "resource_type": item.resource_type,
                    "item_id": item.item_id,
                    "platform": item.platform,
                    "source_url": item.source_url,
                    "error": result.error,
                },
            )

        results.append(result)

    return results


__all__ = [
    "EventCallback",
    "_make_resolver",
    "_select_resolver_for_resource_type",
    "run_fetch",
]

