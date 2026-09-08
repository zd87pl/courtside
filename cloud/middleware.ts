import { NextRequest, NextResponse } from 'next/server';

/** The independent prototype stays behind an operator-controlled access gate. */
export async function middleware(req: NextRequest) {
  if (process.env.NODE_ENV !== 'production') return NextResponse.next();
  const secret = process.env.CLOUD_ACCESS_TOKEN;
  if (!secret || secret.length < 32 || !process.env.APP_ORIGIN) {
    return new NextResponse('Private prototype: configure CLOUD_ACCESS_TOKEN and APP_ORIGIN.', { status: 503 });
  }
  const actual = req.headers.get('authorization') || '';
  const expected = 'Basic ' + btoa('courtside:' + secret);
  const digest = async (value: string) => new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value)));
  const [a, b] = await Promise.all([digest(actual), digest(expected)]);
  let difference = 0;
  for (let i = 0; i < a.length; i++) difference |= a[i] ^ b[i];
  if (difference) return new NextResponse('Authentication required', {
    status: 401, headers: { 'WWW-Authenticate': 'Basic realm="Courtside private prototype"' },
  });
  const response = NextResponse.next();
  response.headers.set('Cache-Control', 'private, no-store');
  return response;
}

export const config = { matcher: ['/((?!_next/static|_next/image|favicon.ico).*)'] };
