# 实验：批处理 + 减少中间 `view_layer.update()`（未合并）

> 分支 `experiments/batch-update`，基于 `main@22012d1`。**不改产品行为**，只放测量脚本、原型与改造提案。

## 结论

1. **贵的是 `view_layer.update()`，不是数据写入**：真实成品场景里单次 update
   **行星发动机 ~1.07 ms**、**太空电梯 ~8.07 ms**（2k 对象 / 5.7M 顶点）。
2. **同一组「3 阶段 × 300 对象」写入**：
   - 每对象每阶段 update：行星 959.5 ms；太空电梯 **7,266.8 ms**
   - 每阶段 update（3 次）：行星 7.6 ms；太空电梯 41.5 ms
   - 最后 update 一次：行星 4.3 ms；太空电梯 **16.2 ms**
   - 只写不 update：行星 1.5 ms；太空电梯 6.0 ms
   - 四种写法最终状态哈希**完全一致**（批量不改变结果）。
   - 折算：行星 **223×**、太空电梯 **448×**（这条链路的理论上限）。
3. **静态面**：`runtime/*.py` 里 `view_layer.update()` 共 **45 处**；其中 **6 处在循环体内**：
   `assembly.py:asm_repair`、`motion.py:_restore_transforms`（2 处）、`motion.py:_measure_one`、
   `qc_render.py:qc_render_views`、`qc_render.py:_selftest_projection`（后两处是测试辅助）。
   逐文件计数直接跑 `analyze_updates.py`（输出 JSON）。
4. **收益边界**：上面是"纯数据写入"的上限。真实 op 往往在阶段之间要读 evaluated 数据
   （BVH、`to_mesh()`、自交检测、渲染快照），这些点**必须**保留 update；能省的是
   「连续写多步后才读」和「循环里每对象写+update」两类。

## 文件

| 文件 | 作用 |
|---|---|
| `analyze_updates.py` | 静态分析 `runtime/*.py` 的 update 调用点（文件/函数/是否在循环内），输出 JSON |
| `bench_batch.py` | A/B 基准：真实 `.blend` 上跑 per_object / per_stage / once / write_only 四种写法，并校验最终状态哈希 |
| `batch_executor.py` | 批量 update 的最小原型（`BatchUpdates` 上下文管理器），供改造时参考 |

## 复跑

```bash
# 静态清单
python3 experiments/batch-update/analyze_updates.py

# A/B（DSH 里最省事：blender_rt_headless(file=...)，或直接）
blender -b --factory-startup --python experiments/batch-update/bench_batch.py
# 结果：D:/DSH/blender/tmp/batch_update_bench.json
```

## 改造提案（建议分三步，每步都可单独回退）

### 第 1 步：runtime 内加 `BatchUpdates` 工具（无行为变化）
把 `batch_executor.py` 挪到 `runtime/update_batch.py`（或 `runtime/kit` 里），先只用于
**新代码**；老调用点暂不动。此时只增加一个可选工具，零风险。

### 第 2 步：按清单合并「连续 update」与「循环内 update」
优先处理（按收益/风险排序）：

| 位置 | 现状 | 建议 |
|---|---|---|
| `assembly.py:asm_repair`（循环内 update） | 每轮修复后 update，再重扫 | 每轮末尾仍要 update（下一轮 scan 要读 evaluated）；但可把**同一轮内的多次写入**合并成一次 |
| `motion.py:_measure_one`（循环内 update） | 测每个关节前 update | 只保留"测量前"一次；同一对象多关节合并 |
| `assembly.py` / `blockout.py` / `audit.py` / `txn.py` 的一次性 update | 多处相邻 | 同一函数内若两次 update 之间没有 evaluated 读，删掉前一次 |
| `qc_render.py:qc_render_views`（循环内） | 每视角 update | 视角切换本身要重绘/重算，**不建议动**；只合并同视角内的写入 |

判断规则（改每一处前先问）：
1. 下一次读的是 **evaluated 数据**（`evaluated_get` / `to_mesh` / BVH / bake / render）吗？
   是 → 读之前必须有 flush；否 → update 可能多余。
2. 中间有 **算子** 吗？算子可能自己触发求值/undo/depsgraph tag，删掉显式 update 未必省。
3. 有没有依赖 `depsgraph_update_post` 的逻辑（活动追踪 / render_guard / 自动保存）？
   合并 update 会减少该 handler 的触发次数，可能影响"人在 Blender 里操作"的识别粒度。

### 第 3 步：addon 侧 `batch` 命令（省往返，独立于 update 合并）
现状一条命令一个 timer tick。可在 addon 协议加可选命令：

```json
{"type": "batch", "params": {"steps": [{"code": "..."}, {"code": "..."}], "update": "final"}}
```

- 一个 tick 内顺序执行 steps，**默认只在最后 flush 一次**；
- 回执逐 step 给 `{ok, stdout, error}`，任一 step 失败可继续或中止（建议 `on_error: "stop" | "continue"`）；
- 租约/超时/场景版本按整个 batch 计一次（不是每 step）；
- 对模型侧：可作为 `blender_rt_do` 的一个可选 `steps` 参数，而不是新增工具（避免扩大工具面）。

风险：错误定位从"一条命令"变成"batch 内第 k 步"；undo 语义要明确（整批一次 undo 还是按 step）；
超时是整批还是每步；这些都要在协议层写死。

## 实测数据（2026-10-23，Blender 5.2.3 LTS，Windows）

| 场景 | 对象/顶点 | 每次 update | 3 阶段×300 对象 per_object | per_stage | once | 上限倍数 |
|---|---|---|---|---|---|---|
| 行星发动机 | 503 网格 / 48.7k 顶点 | 1.07 ms | 959.5 ms | 7.6 ms | 4.3 ms | 223× |
| 太空电梯 | 1,985 网格 / 5.76M 顶点 | 8.07 ms | 7,266.8 ms | 41.5 ms | 16.2 ms | 449× |

四种模式的最终状态哈希一致（`hash_consistent: true`），说明合并 update 不改变这批写入的结果。

## 不做什么

- **不**全局删 update：读 evaluated 数据前不 flush 会静默拿旧几何，这类 bug 很难在回归里抓到；
- **不**动 `qc_render_views` 的每视角 update（视角变化本身需要重求值）；
- **不**在未测量前改 undo / 渲染 / bake 路径。
