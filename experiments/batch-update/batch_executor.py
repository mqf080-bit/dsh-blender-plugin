# -*- coding: utf-8 -*-
"""批量 update 的最小原型（供 runtime 内部改造时参考，不是已接入的产品代码）。

问题：数据写入本身很便宜，`bpy.context.view_layer.update()` 才贵，而且只在需要读
evaluated 数据前才必要。现在 runtime 里有 45 处显式 update，6 处在循环体里 ——
其中一部分是"每对象写一次就 update 一次"。

用法：

    from batch_executor import BatchUpdates

    with BatchUpdates() as batch:
        for ob in objs:
            ob.location.x += 1.0          # 纯数据写入，不 update
        # 只有需要读 evaluated 数据时才 flush：
        batch.flush()
        bvh = BVHTree.FromObject(ob, depsgraph)  # 读前必须已 flush

注意：
- flush 会触发 `depsgraph_update_post`，依赖该事件的逻辑（活动追踪 / render_guard）会看到一次事件而不是 N 次；
- 任何 `evaluated_get()` / `to_mesh()` / BVH / bake / 渲染快照之前必须 flush，否则读到旧数据；
- 本原型只包 update，不改变算子行为；算子自身仍可能触发求值。
"""
import bpy


class BatchUpdates:
    """把「多步写入 → 一次 flush」显式化；可选统计写入阶段数与 update 次数。"""

    def __init__(self, view_layer=None):
        self.vl = view_layer or bpy.context.view_layer
        self.stages = 0
        self.updates = 0

    def stage(self, fn, *args, **kwargs):
        """执行一个写入阶段（只改数据，不 update）。返回 self 便于链式调用。"""
        result = fn(*args, **kwargs)
        self.stages += 1
        return result

    def flush(self):
        """把当前批次的写入真正推给依赖图；在需要读 evaluated 数据之前调用。"""
        self.vl.update()
        self.updates += 1
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # 异常路径也 flush，避免调用方带着脏依赖图继续跑（与 finally 语义一致）
        self.flush()
        return False
