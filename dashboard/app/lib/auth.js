// Token helpers for the dashboard. Pure functions, framework-free, so they are
// unit-testable and reusable from middleware. The compare does not return early
// on length mismatch.

export function constantTimeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  if (a.length === 0 || b.length === 0) return false;
  let diff = a.length ^ b.length;
  const len = Math.max(a.length, b.length);
  for (let i = 0; i < len; i += 1) {
    diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  }
  return diff === 0;
}

// extractToken(req): the presented token from a Bearer header, else the
// argus_token cookie, else "".
export function extractToken(req) {
  const auth = req.headers.get("authorization") || "";
  if (auth.startsWith("Bearer ")) return auth.slice("Bearer ".length);
  const cookie = req.cookies?.get?.("argus_token");
  return cookie?.value ?? "";
}
