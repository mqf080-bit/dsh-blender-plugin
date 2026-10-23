# -*- coding: utf-8 -*-
"""
live_push_proto.py —— 「事件驱动选中推送」原型 + 与 1.2s 轮询的延迟对比。

必须在 **GUI Blender**（事件循环在跑）里执行；headless `blender -b` 不派发
`bpy.msgbus` 回调与 `bpy.app.timers`，测不了。

做法：
  depsgraph_update_post → 选中指纹变化 → 60 ms 去抖 → 构造 payload → POST /host/selection
  同时：1200 ms / 50 ms 两个轮询定时器模拟「轮询检测」，记录各自相对"动作发生时刻"的延迟。

msgbus 只用于 active / hide_viewport 这类有 RNA 属性的事件；**选中集合订不到**
（Blender 5.2：Object 没有 select RNA 属性），所以检测走 depsgraph。

环境变量：
  PROTO_BACKEND   后端地址，默认 http://127.0.0.1:19877（mock_backend.mjs，避免碰真实 9877）
  PROTO_OUT       结果 JSON；默认 <临时目录>/msgbus_live_push_bench.json
  PROTO_TRIALS    选择变化次数，默认 8
  PROTO_STEP_S    每次间隔秒，默认 2.5

DSH 用户可用 `blender_rt_do(file=<本文件>)` 触发；跑完读 PROTO_OUT（或看 stdout 的
LIVE_PUSH_BENCH_SUMMARY）。脚本结束会删除临时物体/集合、恢复原选中、清 handler / msgbus owner。
"""
import bpy
import json
import os
import sys
import tempfile
import time
import urllib.request

BACKEND = (os.environ.get("PROTO_BACKEND") or "http://127.0.0.1:19877").rstrip("/")
OUT = os.environ.get("PROTO_OUT") or os.path.join(tempfile.gettempdir(), "msgbus_live_push_bench.json")
TRIALS = int(os.environ.get("PROTO_TRIALS") or 8)
STEP_S = float(os.environ.get("PROTO_STEP_S") or 2.5)
# 真实 addon 的 _post_selection_payload() 会读这个变量；默认指向 mock，避免污染真实会话
os.environ["DSH_BLENDER_BACKEND_URL"] = BACKEND

COLL_NAME = "__msgbus_proto__"
TRIAL_NAMES = ("__msgbus_proto_a__", "__msgbus_proto_b__")
PATTERNS = [
    [TRIAL_NAMES[0]],
    [TRIAL_NAMES[0], TRIAL_NAMES[1]],
    [TRIAL_NAMES[1]],
    [],
    [TRIAL_NAMES[0], TRIAL_NAMES[1]],
    [TRIAL_NAMES[0]],
    [],
    [TRIAL_NAMES[1]],
]


def _find_addon():
    """找已加载的 dsh_blender_bridge 模块（复用它的 payload 构造 / POST）。"""
    for name, mod in list(sys.modules.items()):
        if not mod or name.startswith("bpy"):
            continue
        fn = getattr(mod, "build_selection_payload", None)
        if not callable(fn) or not hasattr(mod, "_post_selection_payload"):
            continue
        code = getattr(fn, "__code__", None)
        src = (code.co_filename if code else "") or ""
        if src.replace("\\", "/").endswith("dsh_blender_bridge.py"):
            return mod
    return None


ADDON = _find_addon()


def _minimal_payload():
    """没有装 DSH addon 时的兜底 payload（字段与契约 §S1 同形）。"""
    try:
        selected = list(bpy.context.selected_objects or [])
    except Exception:
        selected = []
    active = getattr(bpy.context.view_layer.objects, "active", None)
    try:
        mode = str(bpy.context.mode)
    except Exception:
        mode = "UNKNOWN"
    return {
        "v": 1,
        "source": "msgbus-proto",
        "sent_at": int(round(time.time() * 1000)),
        "selection": {
            "kind": "objects",
            "count": len(selected),
            "names": [o.name for o in selected],
            "truncated": False,
            "active": active.name if active else None,
            "mode": mode,
            "objects": [],
            "collections": [],
        },
        "scene": {},
        "activity": {},
    }


def _post_minimal(payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BACKEND + "/host/selection", data=data,
                                 headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=3.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(payload):
    if ADDON is not None:
        return ADDON._post_selection_payload(payload, timeout=3.0)
    return _post_minimal(payload)


def _build_payload():
    if ADDON is not None:
        return ADDON.build_selection_payload()
    return _minimal_payload()


def _stats(values):
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    return {
        "n": len(xs),
        "min": round(xs[0], 1),
        "p50": round(xs[len(xs) // 2], 1),
        "p95": round(xs[min(len(xs) - 1, int(len(xs) * 0.95))], 1),
        "max": round(xs[-1], 1),
        "mean": round(sum(xs) / len(xs), 1),
    }


state = {
    "addon_module": getattr(ADDON, "__name__", None),
    "backend": BACKEND,
    "t0": time.perf_counter(),
    "fp": None,
    "changes": [],
    "actions": [],
    "push": [],
    "poll_1200": [],
    "poll_50": [],
    "msgbus_events": [],
    "trials": 0,
    "step": 0,
    "done": False,
    "temp": [],
    "coll": None,
    "saved": None,
    "driver_error": None,
}
owner = object()
_prev_1200 = [None]
_prev_50 = [None]


def _now():
    return round(time.perf_counter() - state["t0"], 6)


def _epoch_ms():
    return int(round(time.time() * 1000))


def _fingerprint():
    try:
        sel = tuple(sorted(o.name for o in (bpy.context.selected_objects or []) if o))
        act = bpy.context.view_layer.objects.active
        return (sel, act.name if act else None)
    except Exception:
        return None


# ── msgbus：订阅 active / hide_viewport（选中没有可订的 RNA 属性）──
def _mk_msgbus_cb(label):
    def cb(*_args):
        state["msgbus_events"].append([label, _now(), _epoch_ms()])
    return cb


msgbus_subs = []
for _label, _key in (
    ("LayerObjects.active", (bpy.types.LayerObjects, "active")),
    ("Object.hide_viewport", (bpy.types.Object, "hide_viewport")),
):
    try:
        bpy.msgbus.subscribe_rna(key=_key, owner=owner, args=(), notify=_mk_msgbus_cb(_label))
        msgbus_subs.append([_label, "OK"])
    except Exception as exc:
        msgbus_subs.append([_label, repr(exc)])


# ── 事件检测：depsgraph_update_post + 指纹 ──
def _on_depsgraph(_scene, _depsgraph=None):
    try:
        fp = _fingerprint()
        if fp is None or fp == state["fp"]:
            return
        state["fp"] = fp
        rec = {"t": _now(), "epoch_ms": _epoch_ms(), "sel": list(fp[0]), "active": fp[1]}
        state["changes"].append(rec)
        state["pending"] = rec
        if not bpy.app.timers.is_registered(_push_once):
            bpy.app.timers.register(_push_once, first_interval=0.06)
    except Exception:
        pass


def _push_once():
    rec = state.pop("pending", None)
    if not rec:
        return None
    t_sent = _now()
    try:
        res = _post(_build_payload())
    except Exception as exc:
        state["push"].append({"t_change": rec["t"], "error": repr(exc)})
        return None
    state["push"].append({
        "t_change": rec["t"],
        "t_sent": t_sent,
        "t_resp": _now(),
        "epoch_change_ms": rec["epoch_ms"],
        "received_at": res.get("received_at"),
        "seq": res.get("seq"),
        "ok": res.get("ok"),
    })
    return None


# ── 轮询对照（相对"动作发生时刻"算延迟，避免与 depsgraph 回调顺序打架）──
def _poll(prev_ref, bucket):
    try:
        fp = _fingerprint()
        if fp is None or fp == prev_ref[0]:
            return
        prev_ref[0] = fp
        t_action = state["actions"][-1] if state["actions"] else None
        if t_action is not None:
            bucket.append({"t_poll": _now(), "t_action": t_action,
                           "latency_ms": round((_now() - t_action) * 1000, 3)})
    except Exception:
        pass


def _poll_1200():
    _poll(_prev_1200, state["poll_1200"])
    return 1.2


def _poll_50():
    _poll(_prev_50, state["poll_50"])
    return 0.05


# ── 临时场景 + 驱动 ──
def _make_temp():
    coll = bpy.data.collections.new(COLL_NAME)
    bpy.context.scene.collection.children.link(coll)
    objs = []
    for name in TRIAL_NAMES:
        mesh = bpy.data.meshes.new(name + "_mesh")
        obj = bpy.data.objects.new(name, mesh)
        coll.objects.link(obj)
        objs.append(obj)
    state["temp"] = [o.name for o in objs]
    state["coll"] = coll.name
    return objs


def _restore():
    saved = state.get("saved") or {}
    try:
        for obj in bpy.context.view_layer.objects:
            obj.select_set(False)
        for name in saved.get("selection", []):
            obj = bpy.data.objects.get(name)
            if obj:
                obj.select_set(True)
        act = bpy.data.objects.get(saved.get("active") or "")
        if act:
            bpy.context.view_layer.objects.active = act
    except Exception:
        pass


def _finish():
    if state["done"]:
        return
    state["done"] = True
    if bpy.app.timers.is_registered(_poll_1200):
        bpy.app.timers.unregister(_poll_1200)
    if bpy.app.timers.is_registered(_poll_50):
        bpy.app.timers.unregister(_poll_50)
    try:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph)
    except Exception:
        pass
    for name in state["temp"]:
        obj = bpy.data.objects.get(name)
        if obj:
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except Exception:
                pass
    coll = bpy.data.collections.get(state.get("coll") or "")
    if coll:
        try:
            bpy.data.collections.remove(coll)
        except Exception:
            pass
    _restore()
    try:
        bpy.msgbus.clear_by_owner(owner)
    except Exception:
        pass

    # depsgraph 检测延迟：每个 change 相对它之前最近的一次 action
    detect = []
    for ch in state["changes"]:
        earlier = [a for a in state["actions"] if a <= ch["t"]]
        if earlier:
            detect.append((ch["t"] - earlier[-1]) * 1000)
    push_lat = []
    for row in state["push"]:
        if row.get("received_at") and row.get("epoch_change_ms"):
            push_lat.append(row["received_at"] - row["epoch_change_ms"])

    summary = {
        "backend": BACKEND,
        "addon_module": state["addon_module"],
        "trials": state["trials"],
        "msgbus_subscriptions": msgbus_subs,
        "msgbus_events": len(state["msgbus_events"]),
        "depsgraph_detect_ms": _stats(detect),
        "push_change_to_server_ms": _stats(push_lat),
        "poll_1200_ms": _stats([r["latency_ms"] for r in state["poll_1200"]]),
        "poll_50_ms": _stats([r["latency_ms"] for r in state["poll_50"]]),
        "changes": state["changes"],
        "actions": state["actions"],
        "push": state["push"],
        "poll_1200": state["poll_1200"],
        "poll_50": state["poll_50"],
        "msgbus_trace": state["msgbus_events"],
        "driver_error": state["driver_error"],
        "saved": state.get("saved"),
    }
    try:
        with open(OUT, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=2)
        print("LIVE_PUSH_BENCH_DONE " + OUT)
    except Exception as exc:
        print("LIVE_PUSH_BENCH_WRITE_ERR " + repr(exc))
    print("LIVE_PUSH_BENCH_SUMMARY " + json.dumps({
        "depsgraph_detect_ms": summary["depsgraph_detect_ms"],
        "push_change_to_server_ms": summary["push_change_to_server_ms"],
        "poll_1200_ms": summary["poll_1200_ms"],
        "poll_50_ms": summary["poll_50_ms"],
        "msgbus_events": summary["msgbus_events"],
    }, ensure_ascii=False))


def _driver():
    try:
        if state["step"] == 0:
            state["saved"] = {
                "selection": [o.name for o in (bpy.context.selected_objects or [])],
                "active": bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None,
            }
            state["objs"] = _make_temp()
            bpy.context.view_layer.update()
            state["fp"] = _fingerprint()
            _prev_1200[0] = state["fp"]
            _prev_50[0] = state["fp"]
            bpy.app.timers.register(_poll_1200, first_interval=1.2, persistent=True)
            bpy.app.timers.register(_poll_50, first_interval=0.05, persistent=True)
            state["step"] = 1
            return STEP_S
        index = state["step"] - 1
        if index < len(PATTERNS) and state["trials"] < TRIALS:
            state["actions"].append(_now())
            wanted = set(PATTERNS[index])
            for obj in bpy.context.view_layer.objects:
                try:
                    obj.select_set(False)
                except Exception:
                    pass
            for obj in state["objs"]:
                try:
                    obj.select_set(obj.name in wanted)
                except Exception:
                    pass
            try:
                bpy.context.view_layer.objects.active = state["objs"][0] if wanted else None
            except Exception:
                pass
            state["trials"] += 1
            state["step"] += 1
            return STEP_S
        _finish()
        return None
    except Exception as exc:
        state["driver_error"] = repr(exc)
        _finish()
        return None


bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph)
bpy.app.timers.register(_driver, first_interval=0.5)
print("LIVE_PUSH_BENCH_STARTED backend=" + BACKEND + " addon=" + str(state["addon_module"])
      + " subs=" + json.dumps(msgbus_subs))
