// consumer_poll.mjs —— 消费者侧轮询测量：每隔 interval 毫秒 GET 一次 /host/selection/latest，
// 记录「后端 received_at → 消费者看到」的延迟（以及原始事件，便于和推送侧对账）。
//
// 用法：node consumer_poll.mjs <interval_ms> <duration_ms> [out.json]
// 后端地址：DSH_BLENDER_BACKEND_URL（默认 http://127.0.0.1:9877）
import fs from 'node:fs';

const BASE = process.env.DSH_BLENDER_BACKEND_URL || 'http://127.0.0.1:9877';
const interval = Number(process.argv[2] || 1200);
const duration = Number(process.argv[3] || 30000);
const out = process.argv[4] || `consumer_${interval}.json`;
const startedAt = Date.now();
const events = [];
let lastSeq = null;

async function poll() {
  try {
    const resp = await fetch(BASE + '/host/selection/latest');
    const body = await resp.json();
    if (body && body.ok === true && body.seq !== lastSeq) {
      lastSeq = body.seq;
      events.push({
        at_ms: Date.now(),
        seq: body.seq,
        received_at: body.received_at,
        unconsumed: body.unconsumed,
        // 后端 received_at → 消费者读完的延迟；跨进程同机时钟可比
        poll_delay_ms: body.received_at == null ? null : Date.now() - body.received_at,
      });
    }
  } catch (e) { /* 后端暂不可达：下一轮再试 */ }
  if (Date.now() - startedAt < duration) setTimeout(poll, interval);
  else {
    const delays = events.filter((e) => e.poll_delay_ms != null).map((e) => e.poll_delay_ms);
    const sorted = delays.slice().sort((a, b) => a - b);
    const stat = (k) => (sorted.length ? sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * k))] : null);
    fs.writeFileSync(out, JSON.stringify({
      backend: BASE, interval, duration, started_at: startedAt,
      summary: delays.length ? {
        n: delays.length, min: sorted[0], p50: stat(0.5), p95: stat(0.95), max: sorted[sorted.length - 1],
        mean: Math.round((delays.reduce((a, b) => a + b, 0) / delays.length) * 10) / 10,
      } : null,
      events,
    }, null, 2));
    console.log('consumer_poll done ' + out + ' events=' + events.length);
  }
}

poll();
