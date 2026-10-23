# msgbus 选中推送原型（实验，不是产品代码）

> 2026-10-23 · 在真实 GUI Blender **5.2.3 LTS** 上实测。结论先行：
> **`bpy.msgbus` 在 Blender 5.2 上订阅不到"选中"**（`Object.select` 不是 RNA 属性）；
> 能订到的是"活动对象"等属性变化（实测 ~0.3 ms 派发）。
> **选中变化**的可靠事件源是 `bpy.app.handlers.depsgraph_update_post`（实测 0.3–0.7 ms）。
> 端到端的大头不在检测，而在消费者侧：插件现状是 **1.2 s 轮询**，端到端中位数 ~0.87 s；
> 换成事件驱动推送 + 50 ms 轮询后，端到端中位数 **~105 ms**。

## 这是什么

| 文件 | 作用 |
|---|---|
| `probe_msgbus.py` | 在 Blender 里确认哪些"选中相关" msgbus key 能订上、哪些能收到通知（2026-10-23 的答案：订阅得上的有 `LayerObjects.active` / `Object.hide_viewport`；`Object.select` 不存在；`LayerObjects.selected` 能订但不触发） |
| `live_push_proto.py` | 事件驱动推送原型：`depsgraph_update_post` 指纹变化 → 60 ms 去抖 → 构造 selection payload → `POST /host/selection`；同时用 1.2 s / 50 ms 两个定时器模拟"轮询检测"并输出对比 |
| `consumer_poll.mjs` | 消费者侧测量：以 1200 ms / 50 ms 轮询 `/host/selection/latest`，记录"后端收到 → 消费者看到"的延迟 |
| `mock_backend.mjs` | 隔离用的最小后端：复用真实 `runtime/selectionstore.mjs`，只实现 `/host/selection*` 三条路由 |

> 为什么要有 mock：第一版直接打真实 `127.0.0.1:9877`，测试用的临时物体选中被正在使用的 Web 客户端消费了
> （`last_consume.how="actions"`）。测量类实验请一律用 mock 隔离。

## 实测结果

环境：Windows 11 · Blender 5.2.3 LTS（GUI）· 插件 v2.6 / addon v1.10 · Node 24 · 本机回环 HTTP · 8 次选中变化。

| 指标 | 1.2 s 轮询（现状） | 事件驱动 + 50 ms 轮询 |
|---|---|---|
| 选中变化 → 检测到 | p50 **815 ms**（均值 765 ms，p95 1107 ms） | **0.6 ms**（depsgraph；0.3–0.7 ms） |
| 检测 → 后端收到（构造 payload + POST） | —（现状还要用户手点 N 面板按钮） | p50 **83 ms**，p95 95 ms |
| 后端收到 → 消费者看到 | p50 788 ms（均值 648 ms，p95 1186 ms） | p50 **28 ms**（p95 36 ms） |
| **端到端：选中变化 → 消费者看到** | p50 **871 ms**，p95 1266 ms，均值 729 ms | p50 **105 ms**，p95 125 ms，均值 106 ms |

### 关键发现

1. **msgbus 不能订选中**：`subscribe_rna(key=(bpy.types.Object, "select"))` 直接报
   `TypeError('subscribe_rna: struct Object does not contain property select')`；
   `Object` 的 RNA 里只有 `hide_select` / `active_selection_set` / `selection_sets`。
   `(bpy.types.LayerObjects, "selected")` 能订阅成功，但选中变化**不触发回调**。
2. **msgbus 能订 active**：`(bpy.types.LayerObjects, "active")` 触发，实测 ~0.3 ms；
   `(bpy.types.Object, "hide_viewport")` 同理。可以作为"活动对象变化"的轻量事件源，但不解决选中集合。
3. **depsgraph 事件足够快**：`depsgraph_update_post` 在选中变化后 0.3–0.7 ms 触发；addon 里本来就有
   `_dshbb_activity_depsgraph_post` 这个钩子，加一个轻量指纹比较即可（不需要新 API）。
4. **推送链路里 payload 构造占大头**：~60–70 ms（`build_selection_payload()` 会跑活动追踪 + 场景元数据），
   HTTP POST 本身个位数 ms。生产实现建议按需构造 / 缓存场景元数据。
5. **只换检测不够**：检测从 815 ms 降到 0.6 ms，但如果消费者还是 1.2 s 轮询，端到端仍是 0–1.2 s 抖动
   （实测 p50 871 ms）。**消费者侧必须改成推送或 ≤100 ms 轮询**，才能拿到 ~105 ms。

## 建议（按改动量排序）

1. **最小改动**：`tui-host` 与 `lib/client.js` 的 `SELECTION_POLL_MS` 从 1200 → 100（本机回环 GET 成本可忽略），
   端到端即可从 p50 ~0.87 s 降到 ~0.1 s。
2. **事件驱动检测**：addon 复用 `depsgraph_update_post` + 指纹（已在跑），不需要 msgbus；
   自动推送做成**默认关闭的开关**（改变"选中即进草稿/上下文"的语义）。
3. **要做真推送**：后端加 SSE（`/host/events`），消费者订阅；按本实测可去掉轮询，端到端 ≈ 检测 + payload + POST。
4. **msgbus 的合适用途**：只订 `LayerObjects.active` / 其它属性做轻量事件，不要指望它覆盖选中集合。

## 复跑

```bash
# 0) 依赖：仓库根目录（runtime/selectionstore.mjs）；Node 18+；GUI Blender 5.2+
cd experiments/msgbus-selection

# 1) 起 mock 后端（隔离，不碰真实 9877）
MOCK_PORT=19877 node mock_backend.mjs &

# 2) 起两个消费者（结果 JSON 落当前目录）
DSH_BLENDER_BACKEND_URL=http://127.0.0.1:19877 node consumer_poll.mjs 1200 45000 consumer_1200.json &
DSH_BLENDER_BACKEND_URL=http://127.0.0.1:19877 node consumer_poll.mjs 50 45000 consumer_50.json &

# 3) 在 GUI Blender 里跑原型（DSH 用户可用 blender_rt_do(file=...)）
#    默认 PROTO_BACKEND=http://127.0.0.1:19877，PROTO_OUT=<临时目录>/msgbus_live_push_bench.json
#    PROTO_TRIALS=8 PROTO_STEP_S=2.5 可调
```

Blender 侧脚本**必须在 GUI（有事件循环）里执行**：headless `-b` 里 msgbus 回调与 `bpy.app.timers`
都不会被派发（实测订阅成功但回调 0 次）——这也是 `blender_rt_worker`/`rt_headless` 不拿来做本测量的原因。

清理：脚本结束时会删除临时物体/集合、恢复原选中、清掉 msgbus owner 与 handler；mock 与消费者进程手动 kill。
