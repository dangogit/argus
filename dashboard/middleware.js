import { NextResponse } from "next/server";
import { constantTimeEqual, extractToken, wantsHtml } from "./app/lib/auth.js";

// Every matched route requires ARGUS_DASHBOARD_TOKEN (Bearer header or
// argus_token cookie). /api/health is always open.
export function middleware(req) {
  const { pathname } = new URL(req.url);
  if (pathname === "/api/health") return NextResponse.next();
  if (pathname === "/api/login" || pathname === "/login") return NextResponse.next();

  const token = process.env.ARGUS_DASHBOARD_TOKEN;
  if (!token) {
    return new NextResponse(JSON.stringify({ ok: false, error: "dashboard_token_required" }), {
      status: 503,
      headers: { "content-type": "application/json" },
    });
  }

  if (constantTimeEqual(extractToken(req), token)) return NextResponse.next();

  console.warn(`argus-dashboard: 401 ${pathname}`);
  if (wantsHtml(req)) {
    // Build the redirect from the forwarded host, not req.url. Behind the
    // Tailscale Serve proxy req.url resolves to Next's internal localhost:3001
    // bind, which is unreachable from the client; x-forwarded-host carries the
    // real external (tailnet) origin.
    const proto = req.headers.get("x-forwarded-proto") || "https";
    const host = req.headers.get("x-forwarded-host") || req.headers.get("host");
    return NextResponse.redirect(`${proto}://${host}/login`, 307);
  }
  return new NextResponse(JSON.stringify({ ok: false, error: "unauthorized" }), {
    status: 401,
    headers: { "content-type": "application/json", "www-authenticate": "Bearer" },
  });
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
