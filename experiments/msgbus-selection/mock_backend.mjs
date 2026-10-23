// mock_backend.mjs —— 隔离测量用的最小后端：复用真实 runtime/selectionstore.mjs，
// 只实现 /host/selection、/host/selection/latest、/host/selection/consume 三条路由。
//
// 为什么需要它：直接打真实 127.0.0.1:9877 时，测试用的临时物体选中会被正在使用的
// Web 客户端消费掉（实践踩过：last_consume.how="actions"）。测量类实验请隔离。
//
// 用法：MOCK_PORT=19877 node mock_backend.mjs
import http from 'node:http';
import { createSelectionStore } from '../../runtime/selectionstore.mjs';

const PORT = Number(process.env.MOCK_PORT || 19877);
const store = createSelectionStore();

function json(res, code, obj, origin) {
  const headers = {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    Vary: 'Origin',
  };
  if (origin) headers['Access-Control-Allow-Origin'] = origin;
  res.writeHead(code, headers);
  res.end(JSON.stringify(obj));
}

const readBody = (req) => new Promise((resolve) => {
  let body = '';
  req.on('data', (chunk) => { body += chunk; });
  req.on('end', () => resolve(body));
});

const server = http.createServer(async (req, res) => {
  const origin = req.headers.origin || null;
  const path = new URL(req.url, 'http://127.0.0.1').pathname;

  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': origin || '*',
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': 'content-type',
    });
    res.end();
    return;
  }
  if (req.method === 'POST' && path === '/host/selection') {
    let payload = null;
    try { payload = JSON.parse(await readBody(req)); } catch { payload = null; }
    const stored = store.put(payload);
    json(res, stored.ok ? 200 : 400, stored, origin);
    return;
  }
  if (req.method === 'GET' && path === '/host/selection/latest') {
    json(res, 200, store.snapshot(), origin);
    return;
  }
  if (req.method === 'POST' && path === '/host/selection/consume') {
    let payload = null;
    try { payload = JSON.parse(await readBody(req)); } catch { payload = null; }
    const out = store.consume(payload && payload.seq, payload || {});
    json(res, out.ok ? 200 : 400, out, origin);
    return;
  }
  json(res, 404, { ok: false, error: 'not found' }, origin);
});

server.listen(PORT, '127.0.0.1', () => console.log('mock backend on http://127.0.0.1:' + PORT));
