// Fixtures derived from grok2api v3.1.8 Web wire parsers, not a live-provider guarantee.
const http = require('node:http');
http.createServer(async (req, res) => {
  const path = req.url.split('?')[0];
  if (!String(req.headers.cookie || '').includes('sso=synthetic-sso')) {
    res.writeHead(401, { 'content-type': 'application/json' });
    return res.end(JSON.stringify({ error: 'synthetic authentication rejected' }));
  }
  let body = req.body;
  if (!body) { let raw = ''; for await (const chunk of req) raw += chunk; try { body = JSON.parse(raw || '{}'); } catch { body = {}; } }
  const json = value => { res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify(value)); };
  if (path === '/api/auth/session') return json({ status: 'authenticated', session: { userId: 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee', email: 'synthetic@example.test' } });
  if (path === '/rest/rate-limits') return json({ remainingQueries: 5, totalQueries: body.modelName === 'auto' ? 7 : 30, windowSizeSeconds: 7200 });
  if (path === '/rest/media/imagine/quota_info') return json(Object.fromEntries(['image', 'imagePro', 'imageEdit', 'video', 'video720p'].map(k => [k, { available: true, remainingQueries: 5, windowSizeSeconds: 7200 }])));
  if (path === '/rest/app-chat/conversations/new') {
    if (body.modelName !== 'imagine-video-gen' || !body.mediaGenInput?.textToVideo || req.headers['x-statsig-id'] !== 'synthetic-signature') {
      res.writeHead(400); return res.end('invalid wire request');
    }
    res.writeHead(200, { 'content-type': 'text/event-stream' });
    res.write('data: ' + JSON.stringify({ result: { response: { streamingVideoGenerationResponse: { progress: 30 } } } }) + '\n\n');
    return res.end('data: ' + JSON.stringify({ result: { response: { streamingVideoGenerationResponse: { progress: 100, videoUrl: 'https://assets.grok.com/synthetic.mp4' } } } }) + '\n\n');
  }
  res.writeHead(501); res.end('Not modeled by native Web fixtures');
}).listen(process.env.PORT || 4564);
