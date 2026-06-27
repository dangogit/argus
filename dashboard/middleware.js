import { NextResponse } from "next/server";
import { constantTimeEqual, extractToken } from "./app/lib/auth.js";

// Every matched route requires ARGUS_DASHBOARD_TOKEN (Bearer header or
// argus_token cookie). /api/health is always open.
export function middleware(req) {
  const { pathname } = new URL(req.url);
  if (pathname === "/api/health") return NextResponse.next();

  const token = process.env.ARGUS_DASHBOARD_TOKEN;
  if (!token) {
    return new NextResponse(JSON.stringify({ ok: false, error: "dashboard_token_required" }), {
      status: 503,
      headers: { "content-type": "application/json" },
    });
  }

  if (constantTimeEqual(extractToken(req), token)) return NextResponse.next();

  console.warn(`argus-dashboard: 401 ${new URL(req.url).pathname}`);
  return new NextResponse(JSON.stringify({ ok: false, error: "unauthorized" }), {
    status: 401,
    headers: { "content-type": "application/json", "www-authenticate": "Bearer" },
  });
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
