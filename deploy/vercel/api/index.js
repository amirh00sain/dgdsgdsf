// Vercel HTTP relay for SpiderPanel's XHTTP transport.
// Relay architecture adapted from the public XHTTP-Installer project:
// https://github.com/avacocloud/XHTTP-Installer (GPL-3.0; see NOTICE-vercel-relay.md).
// This intentionally proxies GET/HEAD/POST HTTP streaming, not WebSocket upgrades.
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';

export const config = {
  api: { bodyParser: false },
  supportsResponseStreaming: true,
  maxDuration: 60,
};

const TARGET_BASE = String(process.env.TARGET_DOMAIN || '').replace(/\/$/, '');
const RELAY_PATH = normalizePath(process.env.RELAY_PATH || '/xhttp-siz10');
const PUBLIC_RELAY_PATH = normalizePath(process.env.PUBLIC_RELAY_PATH || '/xhttp-siz10');
const UPSTREAM_TIMEOUT_MS = 55000;
const MAX_INFLIGHT = 128;
let inFlight = 0;
const STRIP_HEADERS = new Set([
  'host', 'connection', 'proxy-connection', 'keep-alive', 'te', 'trailer',
  'transfer-encoding', 'upgrade', 'forwarded', 'x-forwarded-host',
  'x-forwarded-proto', 'x-forwarded-port', 'x-forwarded-for', 'x-real-ip',
]);

export default async function handler(req, res) {
  if (!TARGET_BASE || !RELAY_PATH || RELAY_PATH === '/' || !PUBLIC_RELAY_PATH || PUBLIC_RELAY_PATH === '/') {
    res.statusCode = 500;
    res.end('Misconfigured Vercel relay');
    return;
  }

  const requestUrl = new URL(req.url || '/', `https://${req.headers.host || 'localhost'}`);
  const path = normalizePath(requestUrl.pathname);
  // Lightweight health endpoint for browser-side ping and deployment checks.
  if (path === '/') {
    res.statusCode = 200;
    res.setHeader('content-type', 'text/plain; charset=utf-8');
    res.setHeader('cache-control', 'no-store');
    res.end('SpiderPanel XHTTP relay is online');
    return;
  }
  if (path !== PUBLIC_RELAY_PATH && !path.startsWith(`${PUBLIC_RELAY_PATH}/`)) {
    res.statusCode = 404;
    res.end('Not Found');
    return;
  }
  if (!['GET', 'HEAD', 'POST'].includes(req.method || '')) {
    res.setHeader('allow', 'GET, HEAD, POST');
    res.statusCode = 405;
    res.end('Method Not Allowed');
    return;
  }

  const suffix = path === PUBLIC_RELAY_PATH ? '' : path.slice(PUBLIC_RELAY_PATH.length);
  const upstreamUrl = `${TARGET_BASE}${RELAY_PATH}${suffix}${requestUrl.search}`;
  const headers = {};
  for (const [key, value] of Object.entries(req.headers || {})) {
    const lower = key.toLowerCase();
    if (STRIP_HEADERS.has(lower) || lower.startsWith('x-vercel-')) continue;
    if (value == null) continue;
    if (['accept', 'accept-encoding', 'accept-language', 'cache-control', 'content-length', 'content-type', 'pragma', 'range', 'referer', 'user-agent'].includes(lower) || lower.startsWith('sec-ch-') || lower.startsWith('sec-fetch-')) {
      headers[lower] = Array.isArray(value) ? value.join(', ') : String(value);
    }
  }
  const clientIp = req.headers['x-real-ip'] || req.headers['x-forwarded-for'];
  if (clientIp) headers['x-forwarded-for'] = Array.isArray(clientIp) ? clientIp.join(', ') : String(clientIp);

  if (inFlight >= MAX_INFLIGHT) {
    res.statusCode = 503;
    res.setHeader('retry-after', '1');
    res.end('Server Busy: Too Many Inflight Requests');
    return;
  }
  inFlight += 1;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS);
  try {
    const options = {
      method: req.method,
      headers,
      redirect: 'manual',
      signal: controller.signal,
    };
    if (req.method !== 'GET' && req.method !== 'HEAD') {
      options.body = Readable.toWeb(req);
      options.duplex = 'half';
    }

    const upstream = await fetch(upstreamUrl, options);
    res.statusCode = upstream.status;
    for (const [key, value] of upstream.headers.entries()) {
      const lower = key.toLowerCase();
      if (lower === 'transfer-encoding' || lower === 'connection' || lower === 'content-encoding') continue;
      try { res.setHeader(key, value); } catch (_) {}
    }
    if (req.method === 'HEAD' || !upstream.body) {
      res.end();
      return;
    }
    await pipeline(Readable.fromWeb(upstream.body), res);
  } catch (err) {
    if (!res.headersSent) {
      res.statusCode = err && err.name === 'AbortError' ? 504 : 502;
      res.end(err && err.name === 'AbortError' ? 'Gateway Timeout' : 'Bad Gateway');
    } else if (!res.writableEnded) {
      res.destroy(err);
    }
  } finally {
    clearTimeout(timeout);
    inFlight = Math.max(0, inFlight - 1);
  }
}

function normalizePath(value) {
  let path = String(value || '').trim();
  if (!path) return '';
  if (!path.startsWith('/')) path = `/${path}`;
  path = path.replace(/\/{2,}/g, '/');
  if (path.length > 1 && path.endsWith('/')) path = path.slice(0, -1);
  return path;
}
