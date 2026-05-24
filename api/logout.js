// GET /api/logout — clear the session cookie and bounce to login page.

export const config = { runtime: 'edge' };

export default function handler(request) {
  return new Response(null, {
    status: 302,
    headers: {
      Location: '/login.html',
      'Set-Cookie': 'auth-session=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0',
    },
  });
}
