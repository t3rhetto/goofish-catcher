"""Exercise the real login HTTP endpoints using a temporary app database/profile."""
import os
import tempfile
import time
from pathlib import Path

root=tempfile.TemporaryDirectory()
os.environ['DATA_DIR']=root.name
from fastapi.testclient import TestClient
from app.main import app

try:
    with TestClient(app) as client:
        assert client.post('/api/auth/setup',json={'password':'isolated-smoke-password'}).status_code==200
        started=time.monotonic()
        r=client.post('/api/fish/login')
        assert r.status_code==200,r.text
        assert time.monotonic()-started<3,'Login start blocked the HTTP request'
        print('LOGIN_HTTP_ACCEPTED',r.json(),flush=True)
        while time.monotonic()-started<105:
            state=client.get('/api/fish/status').json()
            print('LOGIN_HTTP_PROGRESS',state,flush=True)
            if state['phase']=='error':raise RuntimeError(state)
            if state['phase']=='ready':break
            time.sleep(5)
        assert state['phase']=='ready',state
        r=client.get('/api/fish/screen')
        assert r.status_code==200,r.text[:200]
        assert r.headers['content-type']=='image/jpeg'
        assert len(r.content)>10000
        Path('/qa/login-api-smoke.jpg').write_bytes(r.content)
        print('LOGIN_HTTP_SCREEN_OK',len(r.content),'bytes',round(time.monotonic()-started),'seconds',flush=True)
        assert client.post('/api/fish/cancel').status_code==200
        assert client.get('/api/fish/status').json()['phase']=='idle'
        assert client.get('/api/fish/screen').status_code==409
        print('LOGIN_HTTP_CANCEL_OK',flush=True)
finally:
    root.cleanup()
