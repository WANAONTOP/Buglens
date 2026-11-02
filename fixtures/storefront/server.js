// Storefront fixture server: dependency-free static host (Node stdlib only).
// Serves a fixed allowlist of files; deterministic output, no credentials.
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC = path.join(HERE, 'public');
const PORT = Number(process.env.STOREFRONT_PORT || 4103);

const ROUTES = {
  '/checkout': 'checkout.html',
  '/checkout-control': 'checkout-control.html',
  '/site.css': 'site.css',
};
const MIME = { '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8' };

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url || '/', 'http://127.0.0.1');
    if (url.pathname === '/healthz') {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end('{"status":"ok","fixture":"storefront"}');
      return;
    }
    const file = ROUTES[url.pathname];
    if (!file) {
      res.writeHead(404, { 'content-type': 'text/plain' });
      res.end('not found');
      return;
    }
    const body = await fs.readFile(path.join(PUBLIC, file));
    res.writeHead(200, { 'content-type': MIME[path.extname(file)] });
    res.end(body);
  } catch {
    res.writeHead(500, { 'content-type': 'text/plain' });
    res.end('fixture server error');
  }
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`storefront fixture on http://127.0.0.1:${PORT}`);
});
