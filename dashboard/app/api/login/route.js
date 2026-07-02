import { NextResponse } from "next/server";
import {
  constantTimeEqual,
  clientIp,
  isLoginRateLimited,
  recordFailedLogin,
  clearLoginAttempts,
} from "../../lib/auth.js";

export async function POST(req) {
  const expected = process.env.ARGUS_DASHBOARD_TOKEN;
  if (!expected) {
    return new NextResponse("ARGUS_DASHBOARD_TOKEN is not configured.", {
      status: 503,
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  }

  const ip = clientIp(req);
  if (isLoginRateLimited(ip)) {
    return new NextResponse(null, { status: 303, headers: { Location: "/login?error=1" } });
  }

  const form = await req.formData();
  const token = String(form.get("token") || "");
  const ok = constantTimeEqual(token, expected);
  if (ok) clearLoginAttempts(ip);
  else recordFailedLogin(ip);
  // Relative Location: the browser resolves it against the external (tailnet)
  // address-bar URL, not Next's internal localhost:3001 bind behind the
  // Tailscale Serve proxy. An absolute URL built from req.url would point at
  // the unreachable internal host.
  const res = new NextResponse(null, {
    status: 303,
    headers: { Location: ok ? "/" : "/login?error=1" },
  });
  if (ok) {
    res.cookies.set("argus_token", token, {
      httpOnly: true,
      sameSite: "strict",
      secure: process.env.NODE_ENV === "production",
      path: "/",
      maxAge: 60 * 60 * 12,
    });
  }
  return res;
}
