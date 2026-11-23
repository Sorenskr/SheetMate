// 可选免费云端跨网中转服务：部署到 Cloudflare Workers 后，绑定一个 KV 命名空间命名为 SHEETMATE_KV
// 既可用于团队多电脑中心同步，也可在部署后直接转发 /m 或 /api/mobile 路径。
const MAX_BODY_BYTES = 1048576;

function timingSafeEqualStr(a, b) {
  const s1 = String(a || "");
  const s2 = String(b || "");
  const len = Math.max(s1.length, s2.length);
  let diff = s1.length ^ s2.length;
  for (let i = 0; i < len; i++) {
    const c1 = i < s1.length ? s1.charCodeAt(i) : 0;
    const c2 = i < s2.length ? s2.charCodeAt(i) : 0;
    diff |= c1 ^ c2;
  }
  return diff === 0;
}

function resolveCorsHeaders(request, env) {
  const origin = (request.headers.get("Origin") || "").trim();
  const extraRaw = String((env && env.ALLOWED_ORIGINS) || "").trim();
  const allowed = new Set(["https://sorenskr.github.io"]);
  if (extraRaw) {
    for (const part of extraRaw.split(",")) {
      const clean = part.trim();
      if (clean) allowed.add(clean);
    }
  }
  const headers = {
    "Access-Control-Allow-Headers": "Content-Type, X-Hub-Secret, X-Mobile-Token",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    Vary: "Origin",
  };
  if (origin && allowed.has(origin)) {
    headers["Access-Control-Allow-Origin"] = origin;
  }
  return headers;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const cors = resolveCorsHeaders(request, env);
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors });
    }
    const secret = String((env && env.HUB_SECRET) || "sheetmate-hub");
    if (url.pathname.startsWith("/hub/") && !timingSafeEqualStr(request.headers.get("X-Hub-Secret") || "", secret)) {
      return new Response(JSON.stringify({ ok: false, message: "Forbidden" }), {
        status: 403,
        headers: { ...cors, "Content-Type": "application/json; charset=utf-8" },
      });
    }
    if (url.pathname === "/hub/ping") {
      return new Response(JSON.stringify({ ok: true, service: "SheetMateCloudHub" }), {
        headers: { ...cors, "Content-Type": "application/json; charset=utf-8" },
      });
    }
    if (url.pathname === "/hub/state") {
      if (request.method === "GET") {
        const val = (await env.SHEETMATE_KV.get("hub_state")) || "{}";
        return new Response(val, {
          headers: { ...cors, "Content-Type": "application/json; charset=utf-8" },
        });
      }
      if (request.method === "POST") {
        const lenHdr = Number(request.headers.get("Content-Length") || 0);
        if (Number.isFinite(lenHdr) && lenHdr > MAX_BODY_BYTES) {
          return new Response(JSON.stringify({ ok: false, message: "Payload Too Large" }), {
            status: 413,
            headers: { ...cors, "Content-Type": "application/json; charset=utf-8" },
          });
        }
        const body = await request.text();
        if (body.length > MAX_BODY_BYTES) {
          return new Response(JSON.stringify({ ok: false, message: "Payload Too Large" }), {
            status: 413,
            headers: { ...cors, "Content-Type": "application/json; charset=utf-8" },
          });
        }
        let parsed = null;
        try {
          parsed = JSON.parse(body);
        } catch (_e) {
          parsed = null;
        }
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
          return new Response(JSON.stringify({ ok: false, message: "Bad Request: JSON object required" }), {
            status: 400,
            headers: { ...cors, "Content-Type": "application/json; charset=utf-8" },
          });
        }
        await env.SHEETMATE_KV.put("hub_state", JSON.stringify(parsed));
        return new Response(JSON.stringify({ ok: true }), {
          headers: { ...cors, "Content-Type": "application/json; charset=utf-8" },
        });
      }
    }
    return new Response("Not Found", { status: 404, headers: cors });
  },
};
