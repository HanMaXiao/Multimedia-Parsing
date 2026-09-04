"""resource_fetcher.run_parse — 解析阶段编排 (Phase 1 Commit 3).

权威定义: docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md §4 + §5.1 + §7.

设计要点:
  - 逐 URL 调 router.resolve() 选 resolver → resolver.parse() 拿 ResourceItem list.
  - 事件发射 (run_parse 通过注入的 event_cb;CLI 阶段用 make_event_cb 接 stdout).
  - 单 URL 失败不中断整批 (spec §8 部分成功): 失败 / 无匹配 / 空结果都正常 emit + append results.
  - 整批可取消 (spec §8 验收 8): threading.Event set 后,后续 URL 不入 results.

测试用 monkeypatch.setattr(_select_resolver) 替换路由逻辑, 验证:
  - 无匹配 → failed 事件 + error="不支持的链接"
  - 解析成功 → success 事件 + items 透传
  - 解析返空 list (无资源) → skipped 事件
  - 部分成功 / 取消 / event_cb 缺失
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .base import ResourceItem
from .manifest import ResourceParseManifest
from .resolvers.image import ImageResolver
from .resolvers.video import VideoResolver
from .router import resolve as _router_resolve


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class ParseResult:
    """单 URL 解析结果 — 跟 emit 事件 payload 字段对齐."""

    url: str
    items: List[ResourceItem] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def is_success(self) -> bool:
        return self.error is None and len(self.items) > 0


# ---------------------------------------------------------------------------
# 路由选择 (test-exposed, monkeypatch 入口)
# ---------------------------------------------------------------------------


def _select_resolver(url: str) -> Optional[Any]:
    """按 URL 选 resolver instance. 无匹配 → None.

    默认实现: 调 router.resolve() 拿 Resolver class → 实例化 (cookie_file 暂无 batch-level 注入,
    视频 / 图片解析本身不依赖登录态). 测试用 monkeypatch 替换.
    """
    ResolverClass = _router_resolve(url)
    if ResolverClass is None:
        return None
    if ResolverClass is VideoResolver:
        return VideoResolver()
    if ResolverClass is ImageResolver:
        return ImageResolver()
    # 未来 audio / model resolver 加在这里
    return ResolverClass()


# ---------------------------------------------------------------------------
# 事件发射 helper
# ---------------------------------------------------------------------------


EventCallback = Callable[[Dict[str, Any]], None]


def _emit(event_cb: Optional[EventCallback], payload: Dict[str, Any]) -> None:
    """event_cb 缺失时静默 (CLI 单跑模式 + 离线预览场景)."""
    if event_cb is None:
        return
    event_cb(payload)


# ---------------------------------------------------------------------------
# 主体
# ---------------------------------------------------------------------------


def run_parse(
    manifest: ResourceParseManifest,
    *,
    event_cb: Optional[EventCallback] = None,
    cancel_event: Optional[threading.Event] = None,
) -> List[ParseResult]:
    """逐 URL 调 router.resolve() → resolver.parse() → emit parse 事件.

    Parameters
    ----------
    manifest:
        已校验的 ResourceParseManifest (Phase 1 Commit 2 manifest.py 产出).
    event_cb:
        接受 dict payload 的回调. None 时静默. CLI 阶段用 make_event_cb 接 stdout.
    cancel_event:
        threading.Event;set 后当前 URL 收尾,后续 URL 不入 results. None = 不可取消.

    Returns
    -------
    List[ParseResult]:
        长度 = len(manifest.urls);顺序与 urls 一致. 每条带 success / error / skipped 终态.
    """
    results: List[ParseResult] = []
    for url in manifest.urls:
        # 1) 整批取消检查 (URL 启动前)
        if cancel_event is not None and cancel_event.is_set():
            break

        # 2) 路由选 resolver
        resolver = _select_resolver(url)
        if resolver is None:
            _emit(
                event_cb,
                {
                    "type": "resource_fetch_status",
                    "batch_id": manifest.batch_id,
                    "stage": "parse",
                    "status": "failed",
                    "source_url": url,
                    "error": f"不支持的链接: {url}",
                    "message": "没有匹配的 resolver 规则",
                },
            )
            results.append(
                ParseResult(url=url, items=[], error=f"不支持的链接: {url}")
            )
            continue

        # 3) emit processing
        _emit(
            event_cb,
            {
                "type": "resource_fetch_status",
                "batch_id": manifest.batch_id,
                "stage": "parse",
                "status": "processing",
                "source_url": url,
            },
        )

        # 4) 解析 (resolver 内部 try/except 吞异常,返 [] — 防御兜底在编排层再加一层)
        try:
            items = resolver.parse(url)
        except Exception as e:
            _emit(
                event_cb,
                {
                    "type": "resource_fetch_status",
                    "batch_id": manifest.batch_id,
                    "stage": "parse",
                    "status": "failed",
                    "source_url": url,
                    "error": f"parse: {e}",
                },
            )
            results.append(
                ParseResult(url=url, items=[], error=f"parse: {e}")
            )
            continue

        # 5) 解析结果分类: 0 / N
        if not items:
            _emit(
                event_cb,
                {
                    "type": "resource_fetch_status",
                    "batch_id": manifest.batch_id,
                    "stage": "parse",
                    "status": "skipped",
                    "source_url": url,
                    "message": "解析结果为空 (页面无资源 / 解析器拿不到 entry)",
                },
            )
            results.append(ParseResult(url=url, items=[], error=None))
            continue

        # 6) 成功 — emit success + items
        items_payload = [
            {
                "item_id": it.item_id,
                "resource_type": it.resource_type,
                "platform": it.platform,
                "source_url": it.source_url,
                "title": it.title,
                "thumbnail": it.thumbnail,
                "meta": dict(it.meta),
            }
            for it in items
        ]
        # F0.1 终判: items 里的 resource_type 才是权威, 不用 router 的初判
        rtype = items[0].resource_type
        _emit(
            event_cb,
            {
                "type": "resource_fetch_status",
                "batch_id": manifest.batch_id,
                "stage": "parse",
                "status": "success",
                "resource_type": rtype,
                "source_url": url,
                "items": items_payload,
            },
        )
        results.append(ParseResult(url=url, items=items, error=None))

    return results


__all__ = [
    "EventCallback",
    "ParseResult",
    "_select_resolver",
    "run_parse",
]

