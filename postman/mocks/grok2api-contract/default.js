// Transport stand-in for grok2api v3.1.8; it does not simulate model behavior.
const http = require("http");

const server = http.createServer(async (req, res) => {
  const path = new URL(req.url, "http://localhost").pathname;
  if (req.method === "PUT" && /^\/v1\/media\/uploads\/[a-f0-9]{64}$/.test(path)) {
    res.writeHead(204);
    return res.end();
  }
  const auth = req.headers.authorization || req.headers["x-api-key"];
  if (auth !== "Bearer synthetic-grok-key" && auth !== "synthetic-grok-key") {
    res.writeHead(401, { "content-type": "application/json" });
    return res.end(JSON.stringify({ error: { message: "invalid key" } }));
  }
  const reply = (status, value) => {
    res.writeHead(status, { "content-type": "application/json" });
    res.end(JSON.stringify(value));
  };
  if (req.method === "GET" && path === "/v1/models") {
    return reply(200, { object: "list", data: [{ id: "grok-chat-fast", object: "model", owned_by: "grok2api" }] });
  }
  if (req.method === "POST" && path === "/v1/chat/completions") {
    res.writeHead(200, { "content-type": "text/event-stream" });
    res.write('data: {"choices":[{"delta":{"content":"hello"}}]}\n\n');
    return res.end("data: [DONE]\n\n");
  }
  if (req.method === "POST" && path === "/v1/responses") {
    return reply(200, { id: "resp_mock", object: "response", status: "completed", output: [] });
  }
  if (req.method === "GET" && path === "/v1/responses/resp_mock") {
    return reply(200, { id: "resp_mock", object: "response", status: "completed", output: [] });
  }
  if (req.method === "DELETE" && path === "/v1/responses/resp_mock") {
    return reply(200, { id: "resp_mock", deleted: true });
  }
  if (req.method === "POST" && path === "/v1/messages") {
    return reply(200, { id: "msg_mock", type: "message", role: "assistant", content: [], stop_reason: "end_turn" });
  }
  if (req.method === "POST" && ["/v1/images/generations", "/v1/images/edits"].includes(path)) {
    return reply(200, { created: 1, data: [{ b64_json: "c3ludGhldGlj" }] });
  }
  if (req.method === "POST" && path === "/v1/videos/generations") {
    return reply(200, { id: "video_mock", status: "queued" });
  }
  if (req.method === "GET" && path === "/v1/videos/video_mock") {
    return reply(200, { id: "video_mock", status: "completed" });
  }
  if (req.method === "POST" && path === "/v1/audio/speech") {
    res.writeHead(200, { "content-type": "audio/wav" });
    return res.end(Buffer.from("synthetic-audio"));
  }
  if (req.method === "POST" && path === "/v1/audio/transcriptions") {
    return reply(200, { text: "synthetic transcript" });
  }
  return reply(501, { error: { message: "not represented by this stand-in" } });
});
server.listen(process.env.PORT || 4500);
