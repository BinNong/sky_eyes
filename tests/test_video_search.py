"""视频检索（文字找帧）的回归测试。

跑法（只用标准库 + `array`/`math`，**两套环境都能跑**，用后端 venv 最快）：

    server/.venv/bin/python tests/test_video_search.py

默认全部离线：不连编码服务、不读视频、不需要 GPU。
索引是在临时目录里用**手搓的向量**拼出来的，所以断言的是确定的数值。

为什么专门给这块写测试
----------------------
这条链上最危险的失效模式是「**索引写对了没有**」。它有几个特点：

  · **不抛异常**。字节序搞反、dtype 读错、行列错位、写盘截断，全都不报错，
    只会让检索返回"看起来排过序、其实毫无意义"的名次。
  · **肉眼看不出来**。索引是一堆 float32 二进制，打开只会看到乱码。
  · 后果是**排序全错**，而排序就是整个功能的结论。

本项目在等级链上已经吃过三次"静默算错"的亏（中文 `max()` 按码点取错、
裸字符类把「严重」当成等级、`_DEGREE_MAP` 缺「高」），所以这里把四道闸门
全部写成测试：

1. **字节序判断**。`struct.pack(">H", 1)[0] == 0` 这种写法在小端机上恒为真，
   会把所有向量 byteswap 成 NaN —— 真出过这个 bug，这里留一条判别性用例。
2. **归一化不变量**。索引里的向量必须 L2 范数 ≈ 1。这一条同时覆盖字节序、
   dtype、行列错位、截断四种损坏，是性价比最高的一道闸。
3. **回读校验**。`build_index` 写盘后必须自己读回来验一遍，不能写完就宣布成功。
4. **describe() 的键集合**。前端直接读这些键，漏一个不会报错，界面上只是渲染成空白——
   `stride_sec` 就这么漏过一次（显示成「每 秒」）。
"""

from __future__ import annotations

import array
import base64
import json
import math
import os
import shutil
import sys
import tempfile
import traceback
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import video_search as vs  # noqa: E402


# ───────────────────────── 工具 ─────────────────────────


def _unit(values: list[float]) -> list[float]:
    n = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / n for v in values]


def _make_index(dir_: Path, rows: list[list[float]], chips=None, version=None, fps=25.0, stride=5):
    """手搓一个索引目录。向量以**小端 float32** 写入，与 build_index 的约定一致。"""
    dir_.mkdir(parents=True, exist_ok=True)
    dim = len(rows[0])
    flat = array.array("f")
    for r in rows:
        flat.extend(r)
    if vs._needs_byteswap():
        flat.byteswap()
    (dir_ / "vectors.f32").write_bytes(flat.tobytes())

    frames = [
        {"frame_index": i * stride, "time_sec": round(i * stride / fps, 4), "thumb": f"thumbs/f{i*stride:05d}.jpg"}
        for i in range(len(rows))
    ]
    meta = {
        "index_version": vs.INDEX_VERSION if version is None else version,
        "video": "unit-test.mp4",
        "video_path": "/nowhere/unit-test.mp4",
        "fps": fps,
        "duration_sec": len(rows) * stride / fps,
        "source_frames": len(rows) * stride,
        "stride": stride,
        "stride_sec": round(stride / fps, 4),
        "frame_count": len(rows),
        "dim": dim,
        "thumb_width": 320,
        "chips": [c["text"] for c in (chips or [])],
        "chips_cached": len(chips or []),
        "encoder": {"model": "fake"},
        "built_at": "2026-01-01 00:00:00",
        "frames": frames,
    }
    (dir_ / "index.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    rows_chips = []
    for c in chips or []:
        a = array.array("f", c["vec"])
        if vs._needs_byteswap():
            a.byteswap()
        rows_chips.append({"text": c["text"], "vec_b64": base64.b64encode(a.tobytes()).decode()})
    (dir_ / "chips.json").write_text(json.dumps(rows_chips, ensure_ascii=False), encoding="utf-8")
    return dir_


# ───────────────────── ① 字节序（真出过的 bug） ─────────────────────


def test_byteswap_detection_is_not_inverted():
    """`struct.pack(">H", 1)[0] == 0` 在小端机上恒为真 → 会误判成大端。

    这条用例存在的唯一理由：那个写法真的写进过代码，并且**静默**把所有向量
    变成了 NaN。判别方式是拿两种写法对拍——如果哪天有人"优化"回去，这里会红。
    """
    naive = __import__("struct").pack(">H", 1)[0] == 0
    assert naive is True, "前提变了：这个陷阱在当前平台上不再触发，该用例需要重写"
    correct = vs._needs_byteswap()
    assert correct is (sys.byteorder == "big")
    assert correct is False, "本机是小端，应当是 False"
    assert naive != correct, "这正是当初的 bug：朴素写法与本机实际相反"


def test_as_f32_roundtrip_is_exact():
    """_as_f32 写出去的字节，读回来必须逐位相同。"""
    vec = _unit([0.1, -0.25, 3.5, 0.0, 1e-3, 42.0])
    a = vs._as_f32(vec)
    raw = a.tobytes()
    back = array.array("f")
    back.frombytes(raw)
    assert list(back) == list(a)


# ───────────────────── ② 归一化不变量 ─────────────────────


def test_sanity_check_rejects_nan():
    bad = array.array("f", [float("nan")] + [0.0] * 7)
    flat = memoryview(bad.tobytes()).cast("f")
    try:
        vs._sanity_check_rows(flat, 1, 8, "t")
        raise AssertionError("应当拒绝含 NaN 的向量")
    except ValueError as exc:
        assert "NaN" in str(exc)


def test_sanity_check_rejects_non_unit_norm():
    """字节序搞反的典型症状：范数变成天文数字或 0，而不是 1。"""
    swapped = array.array("f", [1.0, 0.0, 0.0, 0.0])
    swapped.byteswap()
    flat = memoryview(swapped.tobytes()).cast("f")
    norm = math.sqrt(sum(v * v for v in flat))
    assert abs(norm - 1.0) > 1e-2, "字节序反转后范数应当明显不是 1（这就是闸门能抓住它的原因）"
    try:
        vs._sanity_check_rows(flat, 1, 4, "t")
        raise AssertionError("应当拒绝非归一化向量")
    except ValueError as exc:
        assert "L2 范数" in str(exc)


def test_sanity_check_rejects_truncated_index():
    # 缓冲里只有一个合法的 8 维单位向量，却声明有 2 行 → 第 1 行长度为 0
    good = array.array("f", _unit([1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]))
    flat = memoryview(good.tobytes()).cast("f")
    assert abs(math.sqrt(sum(v * v for v in flat)) - 1.0) < 1e-6, "前提：第 0 行本身应当是合法的"
    try:
        vs._sanity_check_rows(flat, 2, 8, "t")
        raise AssertionError("应当发现被截断")
    except ValueError as exc:
        assert "截断" in str(exc), f"期望报截断，实得：{exc}"


# ───────────────────── ③ 索引读写 ─────────────────────


def test_load_index_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "idx"
        rows = [_unit([1, 0, 0, 0]), _unit([0, 1, 0, 0]), _unit([0, 0, 1, 0])]
        _make_index(d, rows)
        idx = vs.load_index(d)
        assert idx.count == 3 and idx.dim == 4
        # 逐行比对：读回来的必须就是写进去的那几行
        for i, row in enumerate(rows):
            assert list(idx.vector(i)) == list(array.array("f", row)), f"第 {i} 行不一致"
        assert [f["frame_index"] for f in idx.frames] == [0, 5, 10]
        assert idx.stride == 5


def test_load_index_rejects_unknown_version():
    """结构不认识的索引要拒绝，不能"尽力而为"地算出一个错结果。"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "idx"
        _make_index(d, [_unit([1, 0, 0, 0])], version=vs.INDEX_VERSION + 99)
        try:
            vs.load_index(d)
            raise AssertionError("应当拒绝未知版本")
        except ValueError as exc:
            assert "版本" in str(exc)


def test_load_index_rejects_size_mismatch():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "idx"
        _make_index(d, [_unit([1, 0, 0, 0]), _unit([0, 1, 0, 0])])
        raw = (d / "vectors.f32").read_bytes()
        (d / "vectors.f32").write_bytes(raw[:-4])  # 砍掉最后一个数
        try:
            vs.load_index(d)
            raise AssertionError("应当发现文件尺寸与声明不符")
        except ValueError as exc:
            assert "不符" in str(exc) or "截断" in str(exc)


def test_load_index_missing_raises_filenotfound():
    with tempfile.TemporaryDirectory() as td:
        try:
            vs.load_index(Path(td) / "nope")
            raise AssertionError("应当抛 FileNotFoundError")
        except FileNotFoundError:
            pass


# ───────────────────── ④ describe() 的键集合 ─────────────────────


def test_describe_exposes_every_key_the_frontend_reads():
    """前端直接读这些键。漏一个**不会报错**，界面只是渲染成空白。

    `stride_sec` 就这么漏过一次：索引行显示成「每 秒」，
    看上去像排版问题，实际是 describe() 根本没返回这个字段。
    """
    required = {
        "video",
        "fps",
        "stride",
        "stride_sec",
        "frame_count",
        "source_frames",
        "duration_sec",
        "dim",
        "chips",
        "chips_cached",
        "built_at",
    }
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "idx"
        _make_index(d, [_unit([1, 0, 0, 0]), _unit([0, 1, 0, 0])])
        desc = vs.load_index(d).describe()
    missing = {k for k in required if k not in desc}
    assert not missing, f"describe() 漏了前端要读的键：{missing}"
    assert all(desc[k] is not None for k in required), f"这些键是 None：{[k for k in required if desc[k] is None]}"


# ───────────────────── ⑤ 排序 ─────────────────────


def test_search_vector_ranks_by_cosine():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "idx"
        rows = [
            _unit([1, 0, 0, 0]),   # 与查询完全一致
            _unit([0.9, 0.436, 0, 0]),
            _unit([0, 0, 1, 0]),   # 正交
        ]
        _make_index(d, rows)
        idx = vs.load_index(d)
        hits = idx.search_vector(array.array("f", _unit([1, 0, 0, 0])), top_k=3)
        assert [h.rank for h in hits] == [0, 1, 2], "名次必须从 0 连续起来"
        assert hits[0].score > hits[1].score > hits[2].score
        assert hits[0].frame_index == 0 and hits[0].time_sec == 0.0
        assert abs(hits[2].score) < 1e-6, "正交向量余弦应约为 0"


def test_hit_to_dict_rounds_and_keeps_shape():
    h = vs.Hit(frame_index=615, time_sec=24.6, score=0.39173, rank=0, thumb="thumbs/f00615.jpg")
    d = h.to_dict()
    assert set(d) == {"frame_index", "time_sec", "score", "rank", "thumb"}
    assert d["score"] == 0.3917 and d["time_sec"] == 24.6


def test_search_vector_dimension_mismatch_raises():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "idx"
        _make_index(d, [_unit([1, 0, 0, 0])])
        idx = vs.load_index(d)
        try:
            idx.search_vector(array.array("f", [1.0, 0.0]), top_k=1)
            raise AssertionError("维度不符应当报错，不能悄悄算下去")
        except ValueError as exc:
            assert "维度" in str(exc)


# ───────────────────── ⑥ 预置查询（断网兜底） ─────────────────────


def test_cached_chip_query_does_not_touch_the_network():
    """预置查询必须走本地缓存向量 —— 现场断网时这几条还得能用。

    传一个**必定连不通**的地址：如果实现偷偷去调服务，这里就会抛 EncoderUnavailable。
    """
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "idx"
        chip_vec = _unit([1, 0, 0, 0])
        _make_index(
            d,
            [_unit([1, 0, 0, 0]), _unit([0, 1, 0, 0])],
            chips=[{"text": "黄色的小汽车", "vec": chip_vec}],
        )
        idx = vs.load_index(d)
        assert idx.cached_query("黄色的小汽车")
        assert not idx.cached_query("一辆蓝色的自行车")
        hits = idx.search("黄色的小汽车", top_k=2, client=vs.EncoderClient("http://127.0.0.1:9"))
        assert hits and hits[0].frame_index == 0
        assert idx.chips() == ["黄色的小汽车"]


def test_free_text_query_raises_encoder_unavailable_when_service_is_down():
    """非预置查询在服务不可达时必须**明确抛异常**，不能返回空列表。

    「服务不可用」与「没搜到」在界面上都表现为"没有结果"，
    但处置方向完全相反：一个去查隧道，一个去改查询词。
    """
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "idx"
        _make_index(d, [_unit([1, 0, 0, 0])])
        idx = vs.load_index(d)
        try:
            idx.search("一辆蓝色的自行车", top_k=3, client=vs.EncoderClient("http://127.0.0.1:9"))
            raise AssertionError("应当抛 EncoderUnavailable")
        except vs.EncoderUnavailable:
            pass


def test_service_status_never_raises():
    """它给健康检查用，服务挂了也只能如实汇报，不能把调用方带崩。"""
    st = vs.service_status(vs.EncoderClient("http://127.0.0.1:9"))
    assert st["available"] is False
    assert "reason" in st and st["reason"]
    assert st["url"] == "http://127.0.0.1:9"


# ───────────────────── ⑦ 客户端解析 ─────────────────────


def test_unpack_rejects_length_mismatch():
    """服务端说 N×D，实际字节数不符时要拒绝——这是"传输被截断"的唯一迹象。"""
    wrong = base64.b64encode(array.array("f", [0.0] * 6).tobytes()).decode()
    try:
        vs._unpack({"shape": [2, 4], "dtype": "float32", "b64": wrong})
        raise AssertionError("应当拒绝长度不符的向量")
    except vs.EncoderUnavailable as exc:
        assert "长度" in str(exc)


def test_unpack_preserves_exact_values():
    """base64/float32 走一圈必须逐位相同 —— 跨机比对基准就靠它。"""
    vec = _unit([0.123456789, -0.987654321, 1e-8, 12345.6789])
    packed = {
        "shape": [1, 4],
        "dtype": "float32",
        "b64": base64.b64encode(array.array("f", vec).tobytes()).decode(),
    }
    out = vs._unpack(packed)
    assert len(out) == 1
    assert list(out[0]) == list(array.array("f", vec))


def test_client_bypasses_proxy():
    """环回请求必须绕开 HTTP_PROXY。

    本机 `HTTP_PROXY` 指向一个本地代理，走代理访问 127.0.0.1:8001 会失败；
    这是这个环境里反复踩到的坑，所以固化成一条断言。

    注意断言的形态：`build_opener(ProxyHandler({}))` 传**空字典**时，
    ProxyHandler 不给任何协议注册处理函数，于是 build_opener 直接把它优化掉——
    所以「opener 里没有 ProxyHandler」正是我们要的结果，而不是"没配上"。
    """
    os.environ["HTTP_PROXY"] = "http://127.0.0.1:1"
    os.environ["http_proxy"] = "http://127.0.0.1:1"
    try:
        import urllib.request

        c = vs.EncoderClient("http://127.0.0.1:9")
        assert urllib.request.getproxies(), "前提：环境里应当配了代理，否则这条用例没意义"
        kinds = [type(h).__name__ for h in c._opener.handlers]
        assert "ProxyHandler" not in kinds, (
            f"opener 里不该有任何代理处理（实得 {kinds}）——"
            "带代理的话环回请求会被发到代理上而失败"
        )
        # handle_open 里 http/https 只应由 HTTPHandler / HTTPSHandler 处理（没有代理处理器）
        assert c._opener.handle_open["http"][0].__class__.__name__ == "HTTPHandler"
    finally:
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("http_proxy", None)


def test_fmt_sec_is_readable():
    assert vs.fmt_sec(24.6) == "24.60s"
    assert vs.fmt_sec(65.25) == "01:05.25"
    assert vs.fmt_sec(-3) == "0.00s"


# ───────────────────── ⑧ 索引目录约定 ─────────────────────


def test_index_dir_name_is_stable():
    """索引目录名被后端与建索引脚本同时引用，改名会让两边对不上。"""
    assert vs.INDEX_DIRNAME == "search_index"
    assert vs.index_dir_for("/tmp/x").name == "search_index"


def test_default_service_url_is_loopback():
    """默认必须是环回：远端服务靠 SSH 隧道映射到本机端口。"""
    assert vs.DEFAULT_SERVICE_URL.startswith("http://127.0.0.1:")


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
