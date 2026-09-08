import { NextResponse } from "next/server";
import { guardRequest, RequestError } from "./request-guards";
import { AuthError } from "./auth";

type Handler = (req: Request) => Promise<NextResponse>;

/** Uniform JSON error handling for route handlers. */
export function route(handler: Handler): Handler {
  return async (req) => {
    try {
      return await handler(await guardRequest(req));
    } catch (e) {
      if (e instanceof RequestError) return NextResponse.json({ error: e.message }, { status: e.status });
      if (e instanceof AuthError) {
        return NextResponse.json({ error: e.message }, { status: 401 });
      }
      const msg = e instanceof Error ? e.message : "internal error";
      const status = /not set|OPENROUTER/i.test(msg) ? 503 : 400;
      return NextResponse.json({ error: msg }, { status });
    }
  };
}

export const ok = (data: unknown, status = 200) => NextResponse.json(data, { status });
