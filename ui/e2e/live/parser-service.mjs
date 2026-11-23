// Real HTTP parser fixture. Only this external service is controlled; the browser, API,
// queue, workers, parsers, indexes and retrieval all run normally.
import { createServer } from 'node:http';

const port = Number(process.env.RAGCENTRE_E2E_FIXTURE_PORT);
const states = new Map();
function state(key) {
  if (!states.has(key)) states.set(key, { mode: 'ok', requests: 0, completed: 0, waiting: [] });
  return states.get(key);
}
function json(res, status, body) {
  res.writeHead(status, { 'content-type': 'application/json' });
  res.end(JSON.stringify(body));
}
const server = createServer(async (req, res) => {
  try {
    const [key, action] = new URL(req.url, `http://127.0.0.1:${port}`).pathname.split('/').filter(Boolean);
    if (key === 'health') return json(res, 200, { ok: true });
    const s = state(key);
    if (action === 'control') {
      if (req.method === 'POST') {
        const chunks = [];
        for await (const chunk of req) chunks.push(chunk);
        const { mode } = JSON.parse(Buffer.concat(chunks).toString());
        if (!['ok', 'fail', 'hold'].includes(mode)) return json(res, 400, { error: 'invalid mode' });
        s.mode = mode;
        if (mode !== 'hold') s.waiting.splice(0).forEach(resolve => resolve());
      }
      return json(res, 200, { mode: s.mode, requests: s.requests, completed: s.completed, waiting: s.waiting.length });
    }
    if (action === 'info' && key === 'ocr-shim') return json(res, 200, {
      protocol: 'ragcentre.parser/v1', name: 'e2e-ocr-shim', version: '1.0', mime_types: ['application/pdf'], capabilities: {},
    });
    if (action === 'info') return json(res, 200, {
      protocol: 'ragcentre.parser/v1', name: `e2e-parser-${key}`, version: '1.0',
      mime_types: ['text/plain', 'text/markdown'], capabilities: {},
    });
    if (action !== 'parse' || req.method !== 'POST') return json(res, 404, { error: 'not found' });
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const form = await new Request(`http://127.0.0.1:${port}`, {
      method: 'POST', headers: { 'content-type': req.headers['content-type'] }, body: Buffer.concat(chunks),
    }).formData();
    s.requests++;
    if (s.mode === 'hold') await new Promise(resolve => s.waiting.push(resolve));
    if (s.mode === 'fail') return json(res, 422, { error: { message: 'E2E parser maintenance: repair service and retry this run' } });
    // The OCR fixture "reads" the scanned page RAGCentre sends; the engines are tested elsewhere.
    const text = key === 'ocr-shim' ? 'RAGCentre OCR check: scanned page, PUMP 4827, bolts, valve, pressure' : await form.get('file').text();
    s.completed++;
    return json(res, 200, {
      schema_version: '1.0', metadata: {}, pages: [],
      blocks: text.split(/\n\s*\n/).filter(Boolean).map(text => ({ type: 'paragraph', text })),
    });
  } catch (error) {
    json(res, 500, { error: String(error) });
  }
});
server.listen(port, '127.0.0.1');
function shutdown() {
  for (const s of states.values()) s.waiting.splice(0).forEach(resolve => resolve());
  server.close();
  server.closeAllConnections();
}
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
