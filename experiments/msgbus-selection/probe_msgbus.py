# -*- coding: utf-8 -*-
"""
probe_msgbus.py —— 确认 Blender 5.2 上「选中」有没有 msgbus 可订阅面。

结论（2026-10-23 / Blender 5.2.3 LTS，本机实测）：
  · (bpy.types.Object, "select")            → TypeError: struct Object does not contain property select
  · (bpy.types.LayerObjects, "selected")    → 能订阅，但选中变化不触发回调
  · (bpy.types.LayerObjects, "active")      → 能订阅，active 变化触发（~0.3 ms）
  · (bpy.types.Object, "hide_viewport")     → 能订阅，属性变化触发
  · 选中集合的可靠事件源：bpy.app.handlers.depsgraph_update_post（0.3–0.7 ms）

用法：
  · GUI：跑脚本后等 ~1 s，看 stdout（定时器会切换 active / hide_viewport / 选中）
  · headless `-b`：只打印订阅结果（不会派发回调，属预期）
"""
import bpy
import json
import time

owner = object()
hits = []
t0 = time.perf_counter()


def _cb(label):
    def cb(*_args):
        hits.append([label, round(time.perf_counter() - t0, 6)])
    return cb


def _props(type_name):
    try:
        t = getattr(bpy.types, type_name)
        return sorted((p.identifier, p.type) for p in t.bl_rna.properties)
    except Exception as exc:
        return [["__err__", repr(exc)]]


print("BLENDER " + bpy.app.version_string + " background=" + str(bool(bpy.app.background)))
print("Object select-like: " + json.dumps([p for p in _props("Object") if "select" in p[0].lower()]))
print("LayerObjects: " + json.dumps(_props("LayerObjects")))
print("ViewLayer select-like: " + json.dumps([p for p in _props("ViewLayer") if "select" in p[0].lower() or p[0] == "objects"]))

candidates = [
    ("Object.select", (bpy.types.Object, "select")),
    ("LayerObjects.active", (bpy.types.LayerObjects, "active")),
    ("LayerObjects.selected", (bpy.types.LayerObjects, "selected")),
    ("ViewLayer.objects", (bpy.types.ViewLayer, "objects")),
    ("Object.hide_viewport", (bpy.types.Object, "hide_viewport")),
]
subs = []
for label, key in candidates:
    try:
        bpy.msgbus.subscribe_rna(key=key, owner=owner, args=(), notify=_cb(label))
        subs.append([label, "OK"])
    except Exception as exc:
        subs.append([label, repr(exc)])
print("SUBSCRIBE " + json.dumps(subs, ensure_ascii=False))


def _step():
    try:
        if not bpy.data.objects.get("__msgbus_probe_a__"):
            bpy.ops.mesh.primitive_cube_add()
            bpy.context.object.name = "__msgbus_probe_a__"
            bpy.ops.mesh.primitive_cube_add()
            bpy.context.object.name = "__msgbus_probe_b__"
        a = bpy.data.objects["__msgbus_probe_a__"]
        b = bpy.data.objects["__msgbus_probe_b__"]
        hits.clear()
        bpy.context.view_layer.objects.active = a          # msgbus: LayerObjects.active
        a.hide_viewport = True                             # msgbus: Object.hide_viewport
        a.hide_viewport = False
        for obj in (a, b):
            obj.select_set(False)
        a.select_set(True)                                  # 选中：msgbus 收不到，depsgraph 能
        bpy.context.view_layer.update()
        time.sleep(0.05)
        print("HITS_AFTER_ACTIONS " + json.dumps(hits, ensure_ascii=False))
        for name in ("__msgbus_probe_a__", "__msgbus_probe_b__"):
            obj = bpy.data.objects.get(name)
            if obj:
                bpy.data.objects.remove(obj, do_unlink=True)
        bpy.msgbus.clear_by_owner(owner)
        print("PROBE_DONE")
    except Exception as exc:
        print("PROBE_ERR " + repr(exc))
    return None


if bpy.app.background:
    print("PROBE_SKIP_DELIVERY（headless 不派发 msgbus 回调；请在 GUI 里复跑看 HITS_AFTER_ACTIONS）")
    bpy.msgbus.clear_by_owner(owner)
else:
    bpy.app.timers.register(_step, first_interval=0.3)
