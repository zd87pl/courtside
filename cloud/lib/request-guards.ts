export const MAX_BODY_BYTES = 3_500_000;

export async function guardRequest(req: Request): Promise<Request> {
  if (['GET', 'HEAD', 'OPTIONS'].includes(req.method)) return req;
  const expected = new URL(process.env.APP_ORIGIN || req.url).origin;
  if (req.headers.get('origin') !== expected) throw new RequestError('Invalid request origin', 403);
  const length = Number(req.headers.get('content-length') || 0);
  if (length > MAX_BODY_BYTES) throw new RequestError('Request body too large', 413);
  if (!req.body) return req;
  const reader = req.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > MAX_BODY_BYTES) {
        await reader.cancel();
        throw new RequestError('Request body too large', 413);
      }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const body = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
  return new Request(req, { body });
}

export class RequestError extends Error {
  status: number;
  constructor(message: string, status: number) { super(message); this.status = status; }
}
