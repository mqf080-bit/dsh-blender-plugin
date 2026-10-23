# -*- coding: utf-8 -*-
"""批处理 / 减少中间 update 的 A/B 基准（真实成品文件）。

三种写法跑同一组「3 阶段 × N 对象」的数据写入：
  per_object : 每对象每阶段写完后 view_layer.update()  → N*3 次 update
  per_stage  : 每阶段写完全部对象后 update()           → 3 次
  once       : 全部写完后 update()                     → 1 次
  write_only : 只写、不显式 update（在计时外补一次）   → 下界
四种写法的最终状态哈希必须一致（验证批量没有改变结果）。

跑法（GUI/无头均可；无头建议 engine=none）：
  blender -b <file.blend> --python bench_batch.py     # 或 DSH 的 blender_rt_headless(file=...)
结果写 D:/DSH/blender/tmp/batch_update_bench.json，并打印 BENCH_JSON。
"""
import bpy, json, os, time, statistics, hashlib

OUT = r"D:/DSH/blender/tmp/batch_update_bench.json"
FILES = [
    ("行星发动机", r"D:/Blender/行星发动机/行星发动机.blend"),
    ("太空电梯", r"D:/Blender/太空电梯/SE_DELIVERY_MODEL.blend"),
]
N = int(os.environ.get("BATCH_N") or 300)
MODES = ("per_object", "per_stage", "once", "write_only")


def load(path):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.wm.open_mainfile(filepath=path)
    bpy.context.view_layer.update()


def pick(n):
    return [o for o in bpy.data.objects if o.type == "MESH"][:n]


def st_loc(ob):
    loc = ob.location
    ob.location = (loc.x + 0.001, loc.y, loc.z)


def st_rot(ob):
    r = ob.rotation_euler
    ob.rotation_euler = (r.x, r.y + 0.001, r.z)


def st_scale(ob):
    s = ob.scale
    ob.scale = (s.x * 1.0001, s.y * 1.0001, s.z * 1.0001)


STAGES = (st_loc, st_rot, st_scale)


def state_hash(objs):
    h = hashlib.sha256()
    for ob in objs:
        for v in (ob.location, ob.rotation_euler, ob.scale):
            h.update(("%.9f,%.9f,%.9f;" % (v.x, v.y, v.z)).encode())
    return h.hexdigest()[:16]


def run(mode, objs):
    vl = bpy.context.view_layer
    updates = 0
    t = time.perf_counter()
    if mode == "per_object":
        for ob in objs:
            for st in STAGES:
                st(ob)
                vl.update(); updates += 1
    elif mode == "per_stage":
        for st in STAGES:
            for ob in objs:
                st(ob)
            vl.update(); updates += 1
    elif mode == "once":
        for st in STAGES:
            for ob in objs:
                st(ob)
        vl.update(); updates += 1
    elif mode == "write_only":
        for st in STAGES:
            for ob in objs:
                st(ob)
        # 不显式 update：计时外补一次，只为后续读状态
    else:
        raise ValueError(mode)
    ms = (time.perf_counter() - t) * 1000.0
    if mode == "write_only":
        vl.update()
    return {"mode": mode, "objects": len(objs), "updates": updates, "ms": round(ms, 1),
            "ms_per_update": round(ms / updates, 2) if updates else None,
            "state_hash": state_hash(objs)}


RES = {"blender": bpy.app.version_string, "n": N, "files": {}}
for label, path in FILES:
    rec = {"path": path, "exists": os.path.exists(path)}
    if not rec["exists"]:
        rec["error"] = "missing"
        RES["files"][label] = rec
        continue
    try:
        rows = []
        for mode in MODES:
            load(path)
            objs = pick(N)
            rec["objects_available"] = len([o for o in bpy.data.objects if o.type == "MESH"])
            rows.append(run(mode, objs))
        hashes = {r["state_hash"] for r in rows}
        rec["rows"] = rows
        rec["hash_consistent"] = (len(hashes) == 1)
        rec["hashes"] = sorted(hashes)
        rec["speedup_once_vs_per_object"] = round(
            rows[0]["ms"] / rows[2]["ms"], 1) if rows[2]["ms"] else None
        rec["ok"] = True
    except Exception as exc:
        rec["error"] = repr(exc)
    RES["files"][label] = rec

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(RES, f, ensure_ascii=False, indent=2)
print("BENCH_JSON " + json.dumps(RES, ensure_ascii=False))
