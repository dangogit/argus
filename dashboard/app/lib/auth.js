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

export function wantsHtml(req) {
  const accept = req.headers.get("accept") || "";
  return accept.includes("text/html");
}

// Tiny in-memory failed-login limiter, one process-wide Map keyed by client
// IP. Resets on deploy/restart; that's fine, it only needs to slow down
// online guessing against the single static token, not survive restarts.
const LOGIN_ATTEMPT_WINDOW_MS = 5 * 60 * 1000;
const LOGIN_ATTEMPT_MAX = 10;
const loginAttempts = new Map();

export function clientIp(req) {
  const forwarded = req.headers.get("x-forwarded-for") || "";
  return forwarded.split(",")[0].trim() || "unknown";
}

// isLoginRateLimited(ip, now): true once an IP has hit LOGIN_ATTEMPT_MAX
// failed attempts within the trailing window. Call recordFailedLogin only
// after a failed attempt so successful logins never count against the IP.
export function isLoginRateLimited(ip, now = Date.now()) {
  const entry = loginAttempts.get(ip);
  if (!entry) return false;
  if (now - entry.windowStart > LOGIN_ATTEMPT_WINDOW_MS) {
    loginAttempts.delete(ip);
    return false;
  }
  return entry.count >= LOGIN_ATTEMPT_MAX;
}

export function recordFailedLogin(ip, now = Date.now()) {
  const entry = loginAttempts.get(ip);
  if (!entry || now - entry.windowStart > LOGIN_ATTEMPT_WINDOW_MS) {
    loginAttempts.set(ip, { windowStart: now, count: 1 });
    return;
  }
  entry.count += 1;
}

export function clearLoginAttempts(ip) {
  loginAttempts.delete(ip);
}
