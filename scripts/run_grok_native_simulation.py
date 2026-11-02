"""Exercise real curl HTTP/WebSocket transport against local stand-ins only.

Start postman simulation run postman/simulations/grok-web-native.sim.yaml --port 4564,
then pass http://127.0.0.1:4564/web. Uses an isolated source/config/data copy.
"""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading


def child(origin):
    import socket
    from urllib.parse import urlsplit
    from unittest.mock import patch
    from curl_cffi import requests
    from websockets.asyncio.server import serve
    from services.providers.grok_store import get_store
    from services.providers.grok_accounts import parse_import
    from services.providers.grok_web import GrokWeb
    from services.providers.grok_video import validate
    assert urlsplit(origin).hostname in {"127.0.0.1", "localhost"}
    store = get_store()
    store.import_accounts(parse_import("synthetic-sso"))
    store.save_settings({"statsig_value": "synthetic-signature"})
    account = store.account(store.accounts()[0]["id"], secret=True)
    with GrokWeb(account, session=requests.Session(impersonate="chrome", trust_env=False), origin=origin) as client:
        synced = client.sync()
        assert synced["tier"] == "basic" and synced["quotas"]["fast"]["remaining"] == 5
        progress = []
        url, duration = client.video(validate({"prompt":"synthetic","duration":10}), progress.append)
        assert url == "https://assets.grok.com/synthetic.mp4" and duration == 6 and progress
    print("HTTP identity, signed quotas, video serialization, SSE and Basic cap: passed")
    ready = threading.Event()
    state = {}
    async def wire(ws):
        assert 'sso=synthetic-sso' in ws.request.headers.get('Cookie', '')
        if ws.request.path.startswith('/ws/imagine/listen'):
            await ws.recv()
            value = json.loads(await ws.recv())
            request_id = value['item']['content'][0]['requestId']
            await ws.send(json.dumps({'type':'image','id':'image-1','requestId':request_id,'percentage_complete':100,'blob':'c3ludGhldGlj'}))
            await ws.send(json.dumps({'type':'json','id':'image-1','requestId':request_id,'current_status':'completed','moderated':False}))
            return
        initial = json.loads(await ws.recv())
        assert initial['event']['type'] == 'session.create'
        await ws.send(json.dumps({'session_id':'synthetic-session','event':{'type':'session.created'}}))
        await ws.send(json.dumps({'event':{'type':'conversation.attached','conversation':{'id':'synthetic-session'}}}))
        item = json.loads(await ws.recv()); response = json.loads(await ws.recv())
        assert item['event']['type'] == 'conversation.item.create' and response['event']['type'] == 'response.create'
        await ws.send(json.dumps({'event':{'type':'response.chunk','chunk':{'text':{'text':'Hello native','channel':'CHANNEL_ASSISTANT_RESPONSE'}}}}))
        await ws.send(json.dumps({'event':{'type':'response.done','response':{'id':'parent','status':'completed'}}}))
    async def server():
        state['loop'] = asyncio.get_running_loop(); state['stop'] = asyncio.Event()
        async with serve(wire, '127.0.0.1', 0) as listener:
            state['port'] = listener.sockets[0].getsockname()[1]; ready.set(); await state['stop'].wait()
    thread = threading.Thread(target=lambda: asyncio.run(server()), daemon=True); thread.start()
    assert ready.wait(10)
    try:
        from services.protocol.conversation import ConversationRequest
        account = store.account(account['id'], secret=True)
        with GrokWeb(account, session=requests.Session(impersonate="chrome", trust_env=False), origin=f"http://127.0.0.1:{state['port']}") as client:
            events = list(client.chat_events('Hello', 'grok-chat-fast'))
            assert events[-1]['response']['id'] == 'parent'
            assert client.image(ConversationRequest(model='grok-imagine-image', prompt='synthetic')) == b'synthetic'
        print('Real WebSocket chat handshake, text events, image request correlation and closure: passed')
    finally:
        state['loop'].call_soon_threadsafe(state['stop'].set); thread.join(10)
        store.db.close()


if __name__ == '__main__':
    if '--child' in sys.argv:
        child(sys.argv[-1])
    else:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='media2api-native-wire-') as temp:
            target = Path(temp)
            for name in ('api','services','utils'):
                shutil.copytree(root/name, target/name, ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copy(root/'config.example.json', target/'config.json')
            shutil.copy(root/'VERSION', target/'VERSION')
            shutil.copy(__file__, target/'run.py')
            env = {**os.environ, 'PYTHONPATH':str(target), 'STORAGE_BACKEND':'json', 'MEDIA2API_AUTH_KEY':'synthetic-test-key', 'PYTHONIOENCODING':'utf-8'}
            result = subprocess.run([sys.executable, str(target/'run.py'), '--child', sys.argv[-1]], cwd=target, env=env)
            raise SystemExit(result.returncode)
