// 可选免费云端跨网中转服务：部署到 Cloudflare Workers 后，绑定一个 KV 命名空间命名为 SHEETMATE_KV
// 既可用于团队多电脑中心同步，也可在部署后直接转发 /m 或 /api/mobile 路径。
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const cors = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Headers": "Content-Type, X-Hub-Secret, X-Mobile-Token",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    };
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors });
    }
    const secret = env.HUB_SECRET || "sheetmate-hub";
    if (url.pathname.startsWith("/hub/") && (request.headers.get("X-Hub-Secret") || "") !== secret) {
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
        const body = await request.text();
        await env.SHEETMATE_KV.put("hub_state", body);
        return new Response(JSON.stringify({ ok: true }), {
          headers: { ...cors, "Content-Type": "application/json; charset=utf-8" },
        });
      }
    }
    return new Response("Not Found", { status: 404, headers: cors });
  },
};
