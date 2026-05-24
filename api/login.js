// POST /api/login — validate credentials, set signed session cookie, redirect to /

export const config = { runtime: 'edge' };

const SECRET = process.env.AUTH_SECRET || 'dev-only-do-not-use-in-production';
const SITE_USER = process.env.SITE_USER || '';
const SITE_PASS = process.env.SITE_PASS || '';
const SESSION_DAYS = parseInt(process.env.SESSION_DAYS || '7', 10);

function b64UrlEncode(s) {
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

async function hmacB64(message, secret) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    'raw',
    enc.encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign']
  );
  const sigBuf = await crypto.subtle.sign('HMAC', key, enc.encode(message));
  const bytes = new Uint8Array(sigBuf);
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export default async function handler(request) {
  if (request.method !== 'POST') {
    return new Response('Method Not Allowed', { status: 405 });
  }

  let user = '', pass = '';
  const ct = request.headers.get('content-type') || '';
  if (ct.includes('application/x-www-form-urlencoded')) {
    const params = new URLSearchParams(await request.text());
    user = params.get('user') || '';
    pass = params.get('pass') || '';
  } else if (ct.includes('application/json')) {
    try {
      const body = await request.json();
      user = body.user || '';
      pass = body.pass || '';
    } catch (e) {}
  }

  if (!SITE_USER || !SITE_PASS) {
    return new Response('Server not configured (env vars missing)', { status: 500 });
  }

  if (user !== SITE_USER || pass !== SITE_PASS) {
    return Response.redirect(new URL('/login.html?error=1', request.url), 302);
  }

  const exp = Date.now() + SESSION_DAYS * 24 * 60 * 60 * 1000;
  const payload = b64UrlEncode(JSON.stringify({ u: user, exp }));
  const sig = await hmacB64(payload, SECRET);
  const token = `${payload}.${sig}`;

  const maxAge = SESSION_DAYS * 24 * 60 * 60;
  const cookie =
    `auth-session=${token}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=${maxAge}`;

  return new Response(null, {
    status: 302,
    headers: {
      Location: '/',
      'Set-Cookie': cookie,
    },
  });
}
