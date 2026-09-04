---
name: resolver-extension
description: Add a new platform-specific ResourceResolver (or new resource type) to the multimedia_parsing project. Use when user says "add new platform resolver", "support xxx site", "bilibili/youtube-style extension", "create resolver for huggingface/civitai/twitter", "extend image_fetcher to support xxx", "add new resource type (audio/model)", or "写一个 resolver 抓 xxx 平台的视频/图片/音频/模型". Covers 4 templates: thin extension (inherits VideoResolver/ImageResolver), platform-specific dispatcher (cross-type, like XiaohongshuResolver), real resource-type (like AudioResolver), and stub for future extension (like ModelResolver). Do NOT use for fixing existing resolver bugs (use code-review + verification) or changing the router two-step rule (that's spec-level).
---

# Resolver Extension

Add a new `ResourceResolver` to the `multimedia_parsing` project so users can parse / fetch URLs from a new platform or resource type.

This skill is the **canonical extension guide** for the project. It exists because the project is designed to be extended by teammates — every new platform should land in `<1 hour` if you follow the workflow.

## When to use this skill

Trigger phrases:
- "Add a new platform resolver"
- "Support [site] URLs"
- "Bilibili / YouTube style extension"
- "Create resolver for [platform] (huggingface / civitai / tiktok / weibo / etc.)"
- "Add new resource type (audio / model / pdf / etc.)"
- "写一个 resolver 抓 [平台] 的 [资源类型]"

Do **not** use for:
- Fixing an existing resolver's bug → use `code-review` + `systematic-debugging` + verify with a real URL.
- Changing the F0.1 router two-step rule or adding new resource_type enum → spec-level change, update `docs/superpowers/specs/...` first.
- Replacing `video_fetcher` / `image_fetcher` with a new tool → that's a foundation refactor, not a resolver extension.

## Decision tree — pick a template

| If you need to… | Template | Reference | Time |
|---|---|---|---|
| Add a few `meta` fields to an existing yt-dlp-supported video platform (e.g. bilibili弹幕, youtube chapters) | **Template 1: Thin extension** (inherit `VideoResolver` / `ImageResolver`) | `multimedia_parsing/resource_fetcher/resolvers/bilibili.py` | ~30 min |
| Add a new platform that's yt-dlp / gallery-dl supported but you just need to register a domain → existing resolver handles it | **Template 0: One-liner** (no new class, add `ResolverRule` only) | `multimedia_parsing/resource_fetcher/router.py` | ~5 min |
| A platform's URL can produce **multiple resource types** (e.g. xhs explore → video OR imageList) and you need F0.1 secondary judgment | **Template 2: Platform dispatcher** (own class, dispatch to sub-resolvers) | `multimedia_parsing/resource_fetcher/resolvers/xiaohongshu.py` | ~1.5 h |
| Add a **new resource type** (audio / model / pdf / etc.) that the existing fetchers don't cover | **Template 3: Real resource-type** (own class, implement parse + fetch) | `multimedia_parsing/resource_fetcher/resolvers/audio.py` | ~1 h |
| Reserve a resource type for **future extension** by teammates (e.g. model / pdf) | **Template 4: Stub** (own class, parse returns `[]`, fetch returns not-implemented error) | `multimedia_parsing/resource_fetcher/resolvers/model.py` | ~15 min |

## The 8-step workflow

Regardless of which template you pick, follow these 8 steps in order.

### Step 1 — Verify the platform actually works with the chosen tool

Before writing any code, prove the tool can resolve a real URL from the target platform.

```bash
# For video / audio / mixed:
yt-dlp --skip-download --print "%(id)s %(title)s %(duration)s" "<URL>"

# For image:
gallery-dl --no-download "<URL>"

# Or Python:
python -c "import yt_dlp; ydl = yt_dlp.YoutubeDL({'quiet': True}); print(ydl.extract_info('<URL>', download=False))"
```

If this fails, you have a `cookies.txt` / `xsec_token` / login problem — fix that first, **don't write a resolver around broken tooling**.

### Step 2 — Pick the template (decision tree above)

Document your choice in the new file's module docstring.

### Step 3 — Create the resolver file

```text
multimedia_parsing/resource_fetcher/resolvers/<platform>.py
```

Follow the template file (linked in the decision table) **exactly**:
- Module docstring: platform name, 1-line "what it does", reference to spec §4.
- Class docstring: router registration example, platform-specific notes (cookie / xsec_token / 反爬 提醒).
- `__init__` signature: `cookie_file: Optional[Path] = None`, `account_id: Optional[str] = None` (跟基类对齐).
- `parse(url) -> List[ResourceItem]`: try/except 兜底,失败返 `[]` (让 `run_parse` 层发 failed 事件,不要抛).
- `fetch(item, dest) -> FetchResult`: if wrong resource_type, 返 `FetchResult(error=...)`, **不抛**.
- `__all__ = ["YourResolver"]`.

### Step 4 — Add to `resolvers/__init__.py`

Add an import + a name in `__all__`. Maintain alphabetical order (Bilibili before Video before Xiaohongshu before YouTube).

### Step 5 — Register in `router.py`

For **Template 0** (one-liner):
```python
RESOLVER_RULES.append(
    ResolverRule("video", ("newsite.com",), VideoResolver),  # reuse existing
)
```

For **Template 1/2/3/4** (new class):
```python
from .resolvers.<platform> import <Platform>Resolver
# 在 RESOLVER_RULES 适当位置加一行 (按"具体优先"顺序)
ResolverRule("video", ("newsite.com", "n.short"), <Platform>Resolver),
```

The order matters — `RESOLVER_RULES` is first-match-wins. Place **more specific** rules before **more generic** ones.

### Step 6 — Add tests

Create `tests/test_<platform>_resolver.py`. Required test cases (4 minimum):

| # | Test | What it asserts |
|---|---|---|
| 1 | `test_<platform>_parse_success` | parse a known URL → at least 1 item, `resource_type` correct, `item_id` non-empty |
| 2 | `test_<platform>_parse_resolver_error_returns_empty` | monkeypatch the tool to raise → `parse()` returns `[]` (NOT raises) |
| 3 | `test_<platform>_fetch_wrong_resource_type_returns_error` | feed a mismatched `ResourceItem` → `FetchResult(error=...)`, `is_success=False` |
| 4 | `test_<platform>_router_routes_correctly` | `resolve("https://<platform>/...")` returns the new class (or existing if Template 0) |

If the resolver is **inheriting** an existing one (Template 1), also add:
- `test_<platform>_is_subclass_of_<parent>`

For **Template 4 (stub)**, the failure-path test is more important than success:
- `test_<platform>_fetch_returns_not_implemented_error` — assert error message contains "skill" or "stub" so the next dev sees the pointer.

### Step 7 — Update router tests

`tests/test_resource_fetcher_router.py` has parametrize matrices (`VIDEO_URLS` / `IMAGE_URLS` / etc.). Add your platform's URL samples to the right matrix, or create a new matrix + test function for new resource types (see how `AUDIO_URLS` was added with `AudioResolver`).

### Step 8 — Verify (mandatory, all three must pass)

```bash
# 1. 单线程
cd "D:\XiaoProject\Multimedia Parsing"
python -m pytest tests/test_<platform>_resolver.py tests/test_resource_fetcher_router.py -v

# 2. 全量 (单线程)
python -m pytest -m "not slow" --tb=short
# expect: 486 → 490+ passed (4 minimum new tests)

# 3. 全量 (xdist, 用户的日常命令)
python -m pytest -n auto --dist=loadscope -m "not slow" --tb=line
# expect: same number, 0 errors
```

If xdist shows "Different tests were collected between gw0 and gw1", your parametrize is iterating a `frozenset` / `set`. Fix: use `sorted(...)` instead of `list(...)` (see [pre-existing xdist fix](../../Multimedia%20Parsing/progress.md#session-2026-09-04)).

**Real e2e** (optional but strongly recommended for new platforms):
```bash
python -m uvicorn multimedia_parsing.server:app --port 8001
# (use 8001 if 8000 is held by zombie process)
curl -X POST http://127.0.0.1:8001/parse \
  -H "Content-Type: application/json" \
  -d '{"urls": ["https://<platform>/<path>"]}'
# Then poll GET /batches for the batch_id, or GET /events/{batch_id} for SSE stream.
```

Expected: batch ends with `state: "done"`, `success >= 1` (or `skipped` if cookies missing — that's a real-world sandbox limit, not a bug).

## Key constraints (read these before coding)

These constraints are non-negotiable; if you violate them the project won't work.

1. **`parse()` failures must return `[]`, not raise.** The `run_parse` layer (`resource_fetcher/run_parse.py`) translates an empty list into a `failed` event. Raising bubbles up and crashes the batch. See `XiaohongshuResolver._fetch_xhs_note_info` and `VideoResolver.parse` for the canonical try/except shape.

2. **`fetch()` must return `FetchResult`, not raise.** All branches (success, wrong type, missing id, download error, upload error) are encoded in the return value's `error` / `local_path` / `oss_url` fields. Raise → batch crash.

3. **`cookie_file` must be passed as `str(self.cookie_file) if self.cookie_file else None`.** Don't pass `Path` objects to yt-dlp; it expects str. See `VideoResolver.parse` line 64.

4. **Platform name must be yt-dlp's extractor lowercase.** `rv.platform` from `video_fetcher.resolver.resolve` is what frontend cards show. Don't invent your own — use what yt-dlp returns (you can `print(info.get('extractor'))` to check).

5. **`item_id` format: `{platform}_{native_id}`.** For video: `bilibili_BV1xx`. For audio: `soundcloud_track-123`. This is what `FetchResult.is_success` and OSS upload keys depend on. Don't use uuid unless the platform has no stable id (e.g. raw image URLs use uuid4 in `ImageResolver`).

6. **Resource types are an enum: `video` / `image` / `audio` / `model`.** If you need a new type (e.g. `pdf`), update `base.ResourceType` enum + `router` whitelist first — that's a spec-level change, not a resolver extension.

7. **Both-mode OSS failure degrades to local success + `oss_error` field.** Don't propagate OSS failure to user as overall failure when `mode='both'`. See `VideoResolver.fetch` lines 159-180.

## Common pitfalls

| Pitfall | Symptom | Fix |
|---|---|---|
| `frozenset` in parametrize | xdist ERROR: "Different tests were collected between gw0 and gw1" | Use `sorted(VALID_STATUSES)` not `list(VALID_STATUSES)` |
| Forgetting to add new resolver to `__init__.py` `__all__` | `ImportError: cannot import name 'XResolver'` from `multimedia_parsing.resource_fetcher` | Update both `__init__.py` imports and `__all__` |
| `Path` object passed to yt-dlp cookiefile | yt-dlp silently ignores (or crashes on Windows) | `str(cookie_file)` before passing |
| `RESOLVER_RULES` order wrong | Generic rule shadows platform-specific one | Place more specific domains **before** more generic ones |
| `resolve("...")` returns `None` even though domain is registered | `_extract_host` failed (malformed URL / no scheme) | Test with full `https://` URL, add `xhs` short-link mapping if platform uses redirects |
| Tests pass in single-thread but fail in xdist | Same as frozenset pitfall | Run xdist, check parametrize sources for unordered iterables |
| `DownloadError: No video formats found!` from yt-dlp | Platform URL is actually a different resource type (e.g. xhs explore imageList) | Don't wrap in try/except — use Template 2 (platform dispatcher) for F0.1 secondary judgment |
| New resolver passes tests but real e2e returns `skipped` | Sandbox network limitation (cookies / xsec_token / 登录态) | Document the limitation, don't paper over it in tests |

## Reference files

All paths relative to `D:\XiaoProject\Multimedia Parsing`:

| File | Role |
|---|---|
| `multimedia_parsing/resource_fetcher/base.py` | `ResourceResolver` protocol, `ResourceItem`, `FetchDestination`, `FetchResult`, `ResourceType` enum |
| `multimedia_parsing/resource_fetcher/router.py` | `ResolverRule` dataclass, `RESOLVER_RULES` registry, `resolve()` |
| `multimedia_parsing/resource_fetcher/resolvers/__init__.py` | Re-exports (alphabetical) |
| `multimedia_parsing/resource_fetcher/resolvers/video.py` | Template 1 / 0 — `VideoResolver` (parent class for thin extensions) |
| `multimedia_parsing/resource_fetcher/resolvers/image.py` | Template 1 / 0 — `ImageResolver` (parent for image extensions) |
| `multimedia_parsing/resource_fetcher/resolvers/bilibili.py` | Template 1 example — thin extension adding `is_bilibili` meta |
| `multimedia_parsing/resource_fetcher/resolvers/youtube.py` | Template 1 example — thin extension adding `is_youtube` meta (handles `platform='yt'` alias) |
| `multimedia_parsing/resource_fetcher/resolvers/xiaohongshu.py` | Template 2 example — F0.1 dispatcher (note_info → video / image) |
| `multimedia_parsing/resource_fetcher/resolvers/audio.py` | Template 3 example — real resource type (yt-dlp audio-only) |
| `multimedia_parsing/resource_fetcher/resolvers/model.py` | Template 4 example — stub for future implementation |
| `tests/test_resource_fetcher_resolvers.py` | Canonical monkeypatch pattern for resolver tests |
| `tests/test_resource_fetcher_router.py` | URL routing parametrize matrices |
| `docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md` §4 | Authoritative spec — Resolver protocol |
| `progress.md` | Session log of past changes (read "session 2026-09-04" for xhs + resolver extension) |
| `AGENTS.md` | Project-level conventions (shell, test commands, code style) |

## After completing the extension

1. Run the full pytest suite one more time (single-thread + xdist).
2. Update `progress.md` with a new "Session YYYY-MM-DD" section: what template you used, what new domain you registered, test counts before/after, any cookies / xsec_token / sandbox limitations discovered.
3. Update `AGENTS.md` "扩展新平台" section to point to your new resolver (one line per resolver).
4. If you added a new resource type, also update `ResourceType` enum docstring and frontend type union (out of scope for this skill but mention in your commit message / progress log).
