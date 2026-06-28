import { NextResponse } from "next/server";
import { constantTimeEqual } from "../../lib/auth.js";

export async function POST(req) {
  const expected = process.env.ARGUS_DASHBOARD_TOKEN;
  if (!expected) {
    return new NextResponse("ARGUS_DASHBOARD_TOKEN is not configured.", {
      status: 503,
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  }

  const form = await req.formData();
  const token = String(form.get("token") || "");
  const target = new URL(constantTimeEqual(token, expected) ? "/" : "/login?error=1", req.url);
  const res = NextResponse.redirect(target, 303);
  if (constantTimeEqual(token, expected)) {
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
