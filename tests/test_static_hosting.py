"""前端静态托管（SKYEYES_STATIC_DIR）的回归测试。

跑法（需要 fastapi/httpx，用后端 venv）：

    server/.venv/bin/python tests/test_static_hosting.py

默认全部离线：不连任何服务，静态目录是临时建的。

为什么单独测这块
----------------
这一段是「部署到服务器只暴露一个端口」的关键改动——后端顺带把前端发出去。
它有两类**会静默出事**的失效，都不报错：

1. **兜底路由吃掉了 /api**。`/{full_path:path}` 是 Starlette 里匹配一切的形状，
   一旦注册顺序早于 API 路由，所有接口都会返回 index.html，
   前端拿到一段 HTML 当 JSON 解析，报错完全指不到原因。
   这里断言的是**注册顺序**，不是"我记得放后面了"。

2. **路径穿越**。静态根之外的文件（比如项目根的 `server/app.py`）通过
   `../` 或 `a/../../` 被读出来。这里用一个**真实存在于静态根之外的文件**做靶子，
   断言它取不到。

还有一条容易被忽略的：静态目录里没有 index.html 时，**必须只警告、不注册兜底**，
否则会把一个健全的 API 服务变成一个全 404 的服务。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_FILE = ROOT / "server" / "app.py"

sys.path.insert(0, str(ROOT / "server"))
sys.path.append(str(ROOT))

from fastapi import HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402

CATCH_ALL = "/{full_path:path}"
_LOADED = 0


def _load_app(static_dir: Path | str | None):
    """按指定的静态目录**重新加载**一份 app 模块。

    app.py 是在 import 期读 `SKYEYES_STATIC_DIR` 并决定是否注册兜底路由的，
    所以每种场景都必须拿到一份全新的模块对象，不能用同一份改环境变量。
    """
    global _LOADED
    _LOADED += 1
    os.environ.pop("SKYEYES_STATIC_DIR", None)
    if static_dir is not None:
        os.environ["SKYEYES_STATIC_DIR"] = str(static_dir)

    name = f"_skyeyes_app_under_test_{_LOADED}"
    spec = importlib.util.spec_from_file_location(name, APP_FILE)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # 必须先进 sys.modules：FastAPI 解析路由参数注解时会去 sys.modules 找模块
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return mod


def _make_dist(root: Path) -> Path:
    """造一个最小的 dist：index.html + 一个真静态文件 + 一个不存在的资源名。"""
    dist = root / "dist"
    (dist / "data").mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<!doctype html><title>SPA</title>", encoding="utf-8")
    (dist / "data" / "demo.json").write_text('{"ok":true}', encoding="utf-8")
    return dist


def _route_paths(mod) -> list[str]:
    return [getattr(r, "path", "") or "" for r in mod.app.routes]


def _call_handler(mod, full_path: str):
    """直接调用兜底处理函数（绕开 ASGI 栈，省掉一个 httpx 依赖）。"""
    handler = getattr(mod, "_serve_frontend", None)
    assert handler is not None, "兜底路由没注册"
    return asyncio.run(handler(full_path))


# ───────────────────── ① 默认不托管（本地开发流程不许变） ─────────────────────


def test_no_env_means_no_catch_all():
    mod = _load_app(None)
    assert CATCH_ALL not in _route_paths(mod), "没设 SKYEYES_STATIC_DIR 却注册了兜底路由"


def test_api_routes_still_present_when_disabled():
    mod = _load_app(None)
    paths = _route_paths(mod)
    assert "/api/health" in paths
    assert "/api/search" in paths


# ───────────────────── ② 兜底必须在 API 之后 ─────────────────────


def test_catch_all_registered_after_every_api_route():
    """注册顺序是可验证的事实，不能靠"我记得放在文件末尾"。"""
    with tempfile.TemporaryDirectory() as tmp:
        dist = _make_dist(Path(tmp))
        mod = _load_app(dist)
        paths = _route_paths(mod)
        assert CATCH_ALL in paths, "设了静态目录却没注册兜底路由"
        i_catch = paths.index(CATCH_ALL)
        api_before = [p for p in paths[:i_catch] if p.startswith("/api/")]
        api_after = [p for p in paths[i_catch + 1 :] if p.startswith("/api/")]
        assert not api_after, f"有 API 路由被排在兜底之后：{api_after}"
        assert len(api_before) >= 8, f"兜底之前只看到 {len(api_before)} 条 API 路由，可疑"
        # 已有的产物静态挂载也必须排在兜底之前，否则实时产物的图会 404
        assert "/api/runs" in api_before


# ───────────────────── ③ 缺 index.html 时只警告、不注册 ─────────────────────


def test_missing_index_html_does_not_register_catch_all():
    with tempfile.TemporaryDirectory() as tmp:
        dist = Path(tmp) / "dist"
        dist.mkdir()  # 故意不放 index.html
        mod = _load_app(dist)
        assert CATCH_ALL not in _route_paths(mod), "没有 index.html 还注册兜底，会把 API 服务变成全 404"
        # API 仍要可用
        assert "/api/health" in _route_paths(mod)


# ───────────────────── ④ 正常服务文件与 SPA 回退 ─────────────────────


def test_api_namespace_is_not_swallowed_by_spa_fallback():
    """`/api/typo` 必须是 404，**不能**回 index.html。

    回了的话，前端拿到 200 + HTML 再去 JSON.parse，报的是语法错误——
    真正原因（路径少打一个字母）完全看不出来。这是本项目最忌讳的那类失效。
    """
    with tempfile.TemporaryDirectory() as tmp:
        dist = _make_dist(Path(tmp))
        mod = _load_app(dist)
        for path in ("api", "api/nope", "api/jobs/typo", "api/search/v2"):
            try:
                res = _call_handler(mod, path)
            except HTTPException as exc:
                assert exc.status_code == 404, f"{path} 的状态码是 {exc.status_code}，应 404"
                continue
            raise AssertionError(
                f"{path} 被 SPA 兜底接管了（返回 {type(res).__name__}），API 路径写错将无法排查"
            )


def test_real_api_route_still_wins():
    """兜底里那条 API 判断不许影响**真实存在**的 API 路由。"""
    with tempfile.TemporaryDirectory() as tmp:
        dist = _make_dist(Path(tmp))
        mod = _load_app(dist)
        paths = _route_paths(mod)
        for real in ("/api/health", "/api/jobs", "/api/search", "/api/alerts/test"):
            assert real in paths, f"{real} 没注册"


def test_root_serves_index():
    with tempfile.TemporaryDirectory() as tmp:
        dist = _make_dist(Path(tmp))
        res = _call_handler(_load_app(dist), "")
        assert isinstance(res, FileResponse)
        assert Path(res.path).name == "index.html"


def test_spa_deep_route_falls_back_to_index():
    with tempfile.TemporaryDirectory() as tmp:
        dist = _make_dist(Path(tmp))
        mod = _load_app(dist)
        for route in ("events/6", "analytics", "labeling", "system"):
            res = _call_handler(mod, route)
            assert isinstance(res, FileResponse), f"{route} 没回退到 index.html"
            assert Path(res.path).name == "index.html"


def test_real_static_file_is_served():
    with tempfile.TemporaryDirectory() as tmp:
        dist = _make_dist(Path(tmp))
        res = _call_handler(_load_app(dist), "data/demo.json")
        assert isinstance(res, FileResponse)
        assert Path(res.path).name == "demo.json"
        assert Path(res.path).parent.name == "data"


def test_missing_asset_returns_404_not_index():
    """带扩展名却不存在 → 必须 404。回 index.html 会让前端把 HTML 当 JS 解析。"""
    with tempfile.TemporaryDirectory() as tmp:
        dist = _make_dist(Path(tmp))
        mod = _load_app(dist)
        for asset in ("assets/index-abc123.js", "media/detections.mp4", "data/nope.json"):
            try:
                res = _call_handler(mod, asset)
            except HTTPException as exc:
                assert exc.status_code == 404, f"{asset} 的状态码是 {exc.status_code}"
                continue
            raise AssertionError(f"{asset} 不存在却返回了 {type(res).__name__}，应 404")


# ───────────────────── ⑤ 路径穿越 ─────────────────────


def test_path_traversal_cannot_escape_static_root():
    """靶子是**真实存在**于静态根之外的文件，取到就算越界。"""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        dist = _make_dist(base)
        secret = base / "secret.txt"
        secret.write_text("TOPSECRET", encoding="utf-8")
        mod = _load_app(dist)

        attacks = [
            "../secret.txt",
            "a/../../secret.txt",
            "./../secret.txt",
            "data/../../secret.txt",
        ]
        for path in attacks:
            try:
                res = _call_handler(mod, path)
            except HTTPException as exc:
                assert exc.status_code == 404
                continue
            assert isinstance(res, FileResponse)
            served = Path(res.path).resolve()
            assert served != secret.resolve(), f"{path} 读到了静态根之外的文件！"
            assert served.is_relative_to(dist.resolve()), f"{path} 服务了根外的 {served}"


def test_absolute_style_path_is_not_served_from_fs_root():
    """`//etc/passwd` 这类：交给 Path 拼接后不能真的落到 /etc。"""
    with tempfile.TemporaryDirectory() as tmp:
        dist = _make_dist(Path(tmp))
        mod = _load_app(dist)
        for path in ("/etc/passwd", "//etc/passwd", "server/app.py"):
            try:
                res = _call_handler(mod, path)
            except HTTPException:
                continue
            served = Path(res.path).resolve()
            assert served.is_relative_to(dist.resolve()), f"{path} 服务了根外的 {served}"


def test_symlink_escaping_root_is_refused():
    """软链指向根外：解析后不在根内，同样不许发。"""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        dist = _make_dist(base)
        secret = base / "secret.txt"
        secret.write_text("TOPSECRET", encoding="utf-8")
        (dist / "leak.txt").symlink_to(secret)
        mod = _load_app(dist)
        try:
            res = _call_handler(mod, "leak.txt")
        except HTTPException as exc:
            assert exc.status_code == 404
            return
        served = Path(res.path).resolve()
        assert served != secret.resolve(), "软链把根外文件漏出来了"


def _run() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"  ❌ {fn.__name__}")
            print("     " + traceback.format_exc().replace("\n", "\n     ").strip())
    print()
    print(f"{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())
