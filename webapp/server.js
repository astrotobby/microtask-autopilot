const http = require('http');
const fs = require('fs');
const path = require('path');
const { URL } = require('url');

const PORT = Number(process.env.PORT || 3000);
const ROOT = path.join(__dirname, 'public');
const DATA_PATH = path.join(ROOT, 'data.json');
const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon'
};

function readData() {
  return JSON.parse(fs.readFileSync(DATA_PATH, 'utf8'));
}

function send(res, status, body, type = 'text/plain; charset=utf-8') {
  res.writeHead(status, {
    'Content-Type': type,
    'Cache-Control': type.includes('json') ? 'no-cache' : 'public, max-age=300',
    'X-Content-Type-Options': 'nosniff'
  });
  res.end(body);
}

function safeFile(requestPath) {
  const requested = requestPath === '/' ? '/index.html' : requestPath;
  const resolved = path.resolve(ROOT, `.${requested}`);
  return resolved.startsWith(`${ROOT}${path.sep}`) ? resolved : null;
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  if (req.method !== 'GET') return send(res, 405, 'Method Not Allowed');
  if (url.pathname === '/api/health') {
    try {
      const data = readData();
      return send(res, 200, JSON.stringify({ ok: true, snapshot_at: data.generated_at, platforms: data.summary.platforms }), 'application/json; charset=utf-8');
    } catch (error) {
      return send(res, 503, JSON.stringify({ ok: false, error: 'Snapshot unavailable' }), 'application/json; charset=utf-8');
    }
  }
  if (url.pathname === '/api/data') {
    try { return send(res, 200, fs.readFileSync(DATA_PATH), 'application/json; charset=utf-8'); }
    catch (error) { return send(res, 503, JSON.stringify({ ok: false, error: 'Snapshot unavailable' }), 'application/json; charset=utf-8'); }
  }
  const file = safeFile(url.pathname);
  if (!file || !fs.existsSync(file) || fs.statSync(file).isDirectory()) return send(res, 404, 'Not Found');
  return send(res, 200, fs.readFileSync(file), MIME[path.extname(file)] || 'application/octet-stream');
});

server.listen(PORT, '0.0.0.0', () => console.log(`Microtask Autopilot Dashboard listening on ${PORT}`));
