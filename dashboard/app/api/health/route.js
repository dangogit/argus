// Open health endpoint for deploy-gating and readiness checks. Returns 200 with
// a tiny JSON body. Intentionally unauthenticated so a health probe never needs
// the dashboard token; it reveals nothing beyond "the server is up".
export const dynamic = "force-dynamic";

export function GET() {
  return new Response(JSON.stringify({ ok: true, service: "argus-dashboard" }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}
