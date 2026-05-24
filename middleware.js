// Vercel Edge Middleware — gates all routes behind a signed-cookie session.
// Login form lives at /login.html, login API at /api/login, logout at /api/logout.
//
// Required env vars (set in Vercel dashboard):
//   SITE_USER     — login ID
//   SITE_PASS     — login password
//   AUTH_SECRET   — HMAC signing secret (64+ random hex chars recommended)

export const config = {
  // Allow login form, login/logout APIs, favicon, robots; gate everything else.
  matcher: ['/((?!api/|login\\.html$|favicon\\.ico$|robots\\.txt$).*)'],
};

const SECRET = process.env.AUTH_SECRET || 'dev-only-do-not-use-in-production';

function b64UrlDecode(s) {
  s = s.replace(/-/g, '+').replace(/_/g, '/');
  while (s.length % 4) s += '=';
  return atob(s);
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

function redirectToLogin(request) {
  return Response.redirect(new URL('/login.html', request.url), 302);
}

export default async function middleware(request) {
  const cookieHeader = request.headers.get('cookie') || '';
  const match = cookieHeader.match(/(?:^|;\s*)auth-session=([^;]+)/);
  if (!match) return redirectToLogin(request);

  const token = match[1];
  const [payload, sig] = token.split('.');
  if (!payload || !sig) return redirectToLogin(request);

  const expected = await hmacB64(payload, SECRET);
  if (expected !== sig) return redirectToLogin(request);

  try {
    const data = JSON.parse(b64UrlDecode(payload));
    if (!data.exp || Date.now() >= data.exp) return redirectToLogin(request);
  } catch (e) {
    return redirectToLogin(request);
  }

  // Cookie valid → allow request to proceed
}
