import { describe, it, expect } from "vitest";
import { constantTimeEqual, extractToken, wantsHtml } from "./auth.js";

describe("constantTimeEqual", () => {
  it("returns true for equal strings", () => {
    expect(constantTimeEqual("abc123", "abc123")).toBe(true);
  });
  it("returns false for different strings", () => {
    expect(constantTimeEqual("abc123", "abc124")).toBe(false);
  });
  it("returns false for different lengths", () => {
    expect(constantTimeEqual("abc", "abcd")).toBe(false);
  });
  it("returns false when either side is empty", () => {
    expect(constantTimeEqual("", "secret")).toBe(false);
    expect(constantTimeEqual("secret", "")).toBe(false);
  });
});

describe("extractToken", () => {
  const mk = (headers, cookies = {}) => ({
    headers: { get: (k) => headers[k.toLowerCase()] ?? null },
    cookies: { get: (k) => (cookies[k] ? { value: cookies[k] } : undefined) },
  });
  it("reads a Bearer authorization header", () => {
    expect(extractToken(mk({ authorization: "Bearer tok" }))).toBe("tok");
  });
  it("falls back to the argus_token cookie", () => {
    expect(extractToken(mk({}, { argus_token: "cookietok" }))).toBe("cookietok");
  });
  it("returns empty string when neither is present", () => {
    expect(extractToken(mk({}))).toBe("");
  });
});

describe("wantsHtml", () => {
  const mk = (accept) => ({
    headers: { get: (k) => (k.toLowerCase() === "accept" ? accept : null) },
  });

  it("detects browser HTML requests", () => {
    expect(wantsHtml(mk("text/html,application/xhtml+xml"))).toBe(true);
  });

  it("does not redirect API clients", () => {
    expect(wantsHtml(mk("application/json"))).toBe(false);
  });
});
