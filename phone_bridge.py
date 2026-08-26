from __future__ import annotations
import argparse, base64, ctypes, io, json, os, re, shutil, struct, subprocess, threading, time, zlib
from ctypes import wintypes
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

CONTROLLER = os.environ.get('ARCHIE_CONTROLLER_URL', 'http://127.0.0.1:8798').rstrip('/')
PORT = int(os.environ.get('ARCHIE_PHONE_PORT', '8844'))
TOKEN = ''
TOPIC = ''
PUBLIC_URL = ''
TUNNEL_KIND = ''
LOCK = threading.RLock()
BUS = {
    'stream_id': '', 'revision': 0, 'text': '', 'text_active': False,
    'updated': 0.0, 'committed_revision': 0, 'audio_seq': 0, 'event': ''
}
SKIN = {'css': '', 'placeholder': '', 'nodes': []}
STOP = threading.Event()


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str).encode('utf-8')


def jget(path: str, timeout: float = 2.5):
    try:
        with urlopen(Request(CONTROLLER + path, headers={'Accept': 'application/json', 'Cache-Control': 'no-store'}), timeout=timeout) as r:
            raw = r.read(2_000_000)
        value = json.loads(raw.decode('utf-8', 'replace') or '{}')
        return value if isinstance(value, dict) else {'value': value}
    except Exception as exc:
        return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}


def jpost(path: str, payload: dict, timeout: float = 3.0):
    data = _json_bytes(payload)
    req = Request(CONTROLLER + path, data=data, headers={'Content-Type': 'application/json', 'Accept': 'application/json'}, method='POST')
    try:
        with urlopen(req, timeout=timeout) as r:
            raw = r.read(2_000_000)
        value = json.loads(raw.decode('utf-8', 'replace') or '{}')
        return value if isinstance(value, dict) else {'value': value}
    except HTTPError as exc:
        try:
            detail = exc.read(200_000).decode('utf-8', 'replace')
        except Exception:
            detail = str(exc)
        return {'ok': False, 'status': int(getattr(exc, 'code', 0) or 0), 'error': detail}
    except Exception as exc:
        return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}


def current_agent():
    h = jget('/seat/head', 1.5)
    return str(h.get('active_occupant') or 'gpt56sol-interface-live')


def controller_action(payload: dict):
    body = dict(payload)
    body['agent'] = current_agent()
    body['intent'] = str(body.get('intent') or 'phone bridge action')[:260]
    return jpost('/action', body, 35.0 if body.get('action') == 'workspace_patch' else 12.0)


def signal(label: str, proof: str = ''):
    try:
        controller_action({'action': 'signal', 'signal': 'phone-bridge', 'label': label[:180], 'proof': proof[:260]})
    except Exception:
        pass


def safe_seat():
    s = jget('/seat', 2.0)
    reply = s.get('reply') if isinstance(s.get('reply'), dict) else {}
    latest = s.get('latest_input') if isinstance(s.get('latest_input'), dict) else {}
    body = s.get('body') if isinstance(s.get('body'), dict) else {}
    time_atom = s.get('time') if isinstance(s.get('time'), dict) else body.get('time') if isinstance(body.get('time'), dict) else {}
    return {
        'active_occupant': s.get('active_occupant'),
        'input_seq': s.get('input_seq'),
        'output_seq': s.get('output_seq'),
        'reply': {
            'seq': reply.get('seq'), 'stream_id': reply.get('stream_id'), 'revision': reply.get('revision'),
            'text': str(reply.get('text') or '')[:16000], 'chars': reply.get('chars'), 'done': reply.get('done'),
            'occupant': reply.get('occupant'), 'input_id': reply.get('input_id'), 'fault': reply.get('fault'),
            'aborted': reply.get('aborted'), 'first_delta_ns': reply.get('first_delta_ns'), 'finished_ns': reply.get('finished_ns')
        },
        'latest_input': {
            'seq': latest.get('seq'), 'id': latest.get('id'), 'source': latest.get('source'), 'kind': latest.get('kind'),
            'chars': latest.get('chars'), 'sha256': latest.get('sha256'), 'dispatch_ns': latest.get('dispatch_ns'),
            'completed_ns': latest.get('completed_ns')
        },
        'time': time_atom,
        'plaintext_persisted': False,
    }


def set_title(value: str):
    try:
        ctypes.windll.kernel32.SetConsoleTitleW(value)
    except Exception:
        pass


def notify_rendezvous(url: str):
    if not TOPIC:
        return
    message = _json_bytes({'url': url, 'token': TOKEN, 'at': time.time()})
    try:
        req = Request('https://ntfy.sh/' + TOPIC, data=message, headers={'Content-Type': 'text/plain; charset=utf-8', 'User-Agent': 'archie-phone-bridge/2'}, method='POST')
        with urlopen(req, timeout=8) as r:
            r.read(4096)
    except Exception as exc:
        print('rendezvous', type(exc).__name__, exc, flush=True)


def set_public(url: str, kind: str):
    global PUBLIC_URL, TUNNEL_KIND
    clean = url.rstrip('/')
    with LOCK:
        PUBLIC_URL = clean
        TUNNEL_KIND = kind
        BUS['event'] = f'{kind}:{clean}'
    set_title('ARCHIE PHONE ' + clean)
    signal('phone bridge public', f'{kind}:{clean}')
    notify_rendezvous(clean)
    print('PUBLIC', clean, flush=True)


def ensure_cloudflared():
    found = shutil.which('cloudflared.exe') or shutil.which('cloudflared')
    if found:
        return Path(found)
    root = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'ArchiePhone'
    root.mkdir(parents=True, exist_ok=True)
    exe = root / 'cloudflared.exe'
    if exe.is_file() and exe.stat().st_size > 1_000_000:
        return exe
    url = 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe'
    try:
        req = Request(url, headers={'User-Agent': 'archie-phone-bridge/2'})
        with urlopen(req, timeout=35) as r, open(exe, 'wb') as f:
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        if exe.is_file() and exe.stat().st_size > 1_000_000:
            return exe
    except Exception as exc:
        print('cloudflared download', type(exc).__name__, exc, flush=True)
    return None


def tunnel_worker():
    exe = ensure_cloudflared()
    if exe:
        try:
            proc = subprocess.Popen([str(exe), 'tunnel', '--no-autoupdate', '--url', f'http://127.0.0.1:{PORT}'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1)
            pat = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com', re.I)
            for line in proc.stdout or ():
                print('CF', line.rstrip(), flush=True)
                m = pat.search(line)
                if m:
                    set_public(m.group(0), 'cloudflare-quick')
            return
        except Exception as exc:
            print('cloudflared', type(exc).__name__, exc, flush=True)
    signal('phone bridge tunnel unavailable', 'cloudflared failed')


def _chunk(tag: bytes, data: bytes):
    return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)


def gdi_png(max_width=960):
    if os.name != 'nt':
        raise RuntimeError('windows only')
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    user32.GetDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    left, top = int(user32.GetSystemMetrics(76)), int(user32.GetSystemMetrics(77))
    width, height = max(1, int(user32.GetSystemMetrics(78))), max(1, int(user32.GetSystemMetrics(79)))
    scale = min(1.0, float(max_width) / float(width))
    dw, dh = max(1, int(width * scale)), max(1, int(height * scale))
    hdc = user32.GetDC(None)
    memdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, dw, dh)
    old = gdi32.SelectObject(memdc, bmp)
    try:
        gdi32.SetStretchBltMode(memdc, 4)
        if not gdi32.StretchBlt(memdc, 0, 0, dw, dh, hdc, left, top, width, height, 0x00CC0020):
            raise RuntimeError('StretchBlt failed')
        class BIH(ctypes.Structure):
            _fields_ = [('biSize', wintypes.DWORD), ('biWidth', wintypes.LONG), ('biHeight', wintypes.LONG), ('biPlanes', wintypes.WORD), ('biBitCount', wintypes.WORD), ('biCompression', wintypes.DWORD), ('biSizeImage', wintypes.DWORD), ('biXPelsPerMeter', wintypes.LONG), ('biYPelsPerMeter', wintypes.LONG), ('biClrUsed', wintypes.DWORD), ('biClrImportant', wintypes.DWORD)]
        class RGBQ(ctypes.Structure):
            _fields_ = [('b', ctypes.c_ubyte), ('g', ctypes.c_ubyte), ('r', ctypes.c_ubyte), ('x', ctypes.c_ubyte)]
        class BI(ctypes.Structure):
            _fields_ = [('h', BIH), ('c', RGBQ * 1)]
        bmi = BI(); bmi.h.biSize = ctypes.sizeof(BIH); bmi.h.biWidth = dw; bmi.h.biHeight = -dh; bmi.h.biPlanes = 1; bmi.h.biBitCount = 32
        buf = (ctypes.c_ubyte * (dw * dh * 4))()
        rows = gdi32.GetDIBits(memdc, bmp, 0, dh, ctypes.byref(buf), ctypes.byref(bmi), 0)
        if int(rows) != dh:
            raise RuntimeError(f'GetDIBits {rows}/{dh}')
        mv = memoryview(buf).cast('B'); raw = bytearray(); stride = dw * 4
        for y in range(dh):
            raw.append(0); row = mv[y * stride:(y + 1) * stride]
            for x in range(0, stride, 4):
                raw.extend((row[x + 2], row[x + 1], row[x]))
        ihdr = struct.pack('>IIBBBBB', dw, dh, 8, 2, 0, 0, 0)
        return b'\x89PNG\r\n\x1a\n' + _chunk(b'IHDR', ihdr) + _chunk(b'IDAT', zlib.compress(bytes(raw), 1)) + _chunk(b'IEND', b'')
    finally:
        try: gdi32.SelectObject(memdc, old)
        except Exception: pass
        try: gdi32.DeleteObject(bmp); gdi32.DeleteDC(memdc); user32.ReleaseDC(None, hdc)
        except Exception: pass


def screen_bytes():
    try:
        from PIL import ImageGrab
        im = ImageGrab.grab(all_screens=True)
        w, h = im.size
        if w > 1100:
            im = im.resize((1100, max(1, int(h * 1100 / w))))
        out = io.BytesIO(); im.convert('RGB').save(out, format='JPEG', quality=50, optimize=False)
        return 'image/jpeg', out.getvalue()
    except Exception:
        return 'image/png', gdi_png(960)


def auto_commit_worker():
    while not STOP.wait(0.08):
        with LOCK:
            sid = str(BUS.get('stream_id') or '')
            rev = int(BUS.get('revision') or 0)
            text = str(BUS.get('text') or '')
            updated = float(BUS.get('updated') or 0.0)
            committed = int(BUS.get('committed_revision') or 0)
            active = bool(BUS.get('text_active'))
        if not active or not sid or not text or rev <= committed or time.time() - updated < 0.9:
            continue
        result = jpost('/phone/text', {'text': text}, 3.0)
        with LOCK:
            if BUS.get('stream_id') == sid and int(BUS.get('revision') or 0) == rev:
                BUS['committed_revision'] = rev
                BUS['event'] = 'text:auto-commit' if result.get('ok', True) else 'text:auto-commit-refused'


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'ArchiePhone/2'

    def log_message(self, fmt, *args):
        return

    def _origin(self):
        return self.headers.get('Origin', '*') or '*'

    def _token(self):
        auth = self.headers.get('Authorization', '')
        if auth.startswith('Bearer ') and auth[7:] == TOKEN:
            return True
        q = parse_qs(urlparse(self.path).query)
        if (q.get('t') or [''])[0] == TOKEN:
            return True
        try:
            c = SimpleCookie(); c.load(self.headers.get('Cookie', ''))
            return c.get('archie_phone') is not None and c['archie_phone'].value == TOKEN
        except Exception:
            return False

    def _headers(self, code=200, ctype='application/json', length=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        if length is not None:
            self.send_header('Content-Length', str(length))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Access-Control-Allow-Origin', self._origin())
        self.send_header('Vary', 'Origin')
        self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')

    def sendb(self, code, body=b'', ctype='application/json', extra=None):
        self._headers(code, ctype, len(body))
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers()
        if body: self.wfile.write(body)

    def do_OPTIONS(self):
        self._headers(204, 'text/plain', 0); self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/health':
            if not self._token(): self.sendb(401, b'{"ok":false}'); return
            with LOCK: value = {'ok': True, 'public_url': PUBLIC_URL, 'tunnel': TUNNEL_KIND, 'at': time.time()}
            self.sendb(200, _json_bytes(value)); return
        if not self._token():
            self.sendb(401, b'{"ok":false,"error":"unauthorized"}'); return
        if path == '/api/state':
            with LOCK:
                bus = {'active': bool(BUS.get('text_active')), 'event': BUS.get('event'), 'revision': BUS.get('revision'), 'audio_seq': BUS.get('audio_seq')}
                skin = dict(SKIN)
            self.sendb(200, _json_bytes({'ok': True, 'seat': safe_seat(), 'bus': bus, 'event': bus.get('event'), 'skin': skin, 'at': time.time()})); return
        if path == '/api/live-input':
            with LOCK:
                value = {k: BUS.get(k) for k in ('stream_id','revision','text','text_active','updated','committed_revision','audio_seq','event')}
            value['ok'] = True
            self.sendb(200, _json_bytes(value)); return
        if path == '/api/skin':
            with LOCK: value = dict(SKIN)
            self.sendb(200, _json_bytes({'ok': True, 'skin': value})); return
        if path == '/api/screen':
            try:
                ctype, data = screen_bytes(); self.sendb(200, data, ctype)
            except Exception as exc:
                self.sendb(503, _json_bytes({'ok': False, 'error': f'{type(exc).__name__}: {exc}'}))
            return
        if path == '/api/screen.mjpg':
            self._headers(200, 'multipart/x-mixed-replace; boundary=frame', None)
            self.send_header('Connection', 'close'); self.end_headers()
            last_crc = None; last_send = 0.0
            try:
                while not STOP.is_set():
                    ctype, data = screen_bytes(); crc = zlib.crc32(data); now = time.time()
                    if crc == last_crc and now - last_send < 0.8:
                        time.sleep(0.08); continue
                    last_crc, last_send = crc, now
                    head = f'--frame\r\nContent-Type: {ctype}\r\nContent-Length: {len(data)}\r\n\r\n'.encode('ascii')
                    self.wfile.write(head); self.wfile.write(data); self.wfile.write(b'\r\n'); self.wfile.flush()
                    time.sleep(0.11)
            except Exception:
                pass
            return
        self.sendb(404, b'{"ok":false,"error":"not_found"}')

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._token(): self.sendb(401, b'{"ok":false,"error":"unauthorized"}'); return
        n = min(4_000_000, int(self.headers.get('Content-Length') or 0)); raw = self.rfile.read(n) if n else b''
        if path == '/api/session':
            self.sendb(200, b'{"ok":true}', extra={'Set-Cookie': f'archie_phone={TOKEN}; Path=/; HttpOnly; Secure; SameSite=None'}); return
        if path == '/api/text-stream':
            try: value = json.loads(raw.decode('utf-8', 'replace') or '{}')
            except Exception: value = {}
            sid = str(value.get('stream_id') or '')[:120]; text = str(value.get('text') or '')[:24000]; rev = max(0, int(value.get('revision') or 0)); final = bool(value.get('final'))
            if not sid: self.sendb(400, b'{"ok":false,"error":"stream_id"}'); return
            should_commit = False
            with LOCK:
                if sid != BUS.get('stream_id') or rev >= int(BUS.get('revision') or 0):
                    BUS.update({'stream_id': sid, 'revision': rev, 'text': text, 'text_active': not final, 'updated': time.time(), 'event': 'text:live'})
                    should_commit = final and bool(text) and rev > int(BUS.get('committed_revision') or 0)
            result = {'ok': True, 'live': True, 'revision': rev}
            if should_commit:
                accepted = jpost('/phone/text', {'text': text}, 3.0)
                with LOCK:
                    BUS['committed_revision'] = rev; BUS['event'] = 'text:commit'; BUS['text_active'] = False
                result['accepted'] = accepted
            self.sendb(202, _json_bytes(result)); return
        if path == '/api/audio':
            q = parse_qs(urlparse(self.path).query)
            try: rate = max(8000, min(48000, int((q.get('rate') or ['16000'])[0])))
            except Exception: rate = 16000
            with LOCK: BUS['audio_seq'] = int(BUS.get('audio_seq') or 0) + 1; BUS['event'] = 'audio:live'
            accepted = jpost('/phone/audio', {'sample_rate': rate, 'pcm16_base64': base64.b64encode(raw).decode('ascii')}, 2.5)
            self.sendb(202, _json_bytes({'ok': True, 'accepted': accepted})); return
        if path == '/api/skin':
            try: value = json.loads(raw.decode('utf-8', 'replace') or '{}')
            except Exception: value = {}
            with LOCK:
                if 'css' in value: SKIN['css'] = str(value.get('css') or '')[:50000]
                if 'placeholder' in value: SKIN['placeholder'] = str(value.get('placeholder') or '')[:200]
                if isinstance(value.get('nodes'), list): SKIN['nodes'] = value['nodes'][:128]
                BUS['event'] = 'skin:update'
                out = dict(SKIN)
            self.sendb(200, _json_bytes({'ok': True, 'skin': out})); return
        if path == '/api/action':
            try: value = json.loads(raw.decode('utf-8', 'replace') or '{}')
            except Exception: value = {}
            if not isinstance(value, dict) or not value.get('action'):
                self.sendb(400, b'{"ok":false,"error":"action"}'); return
            result = controller_action(value)
            with LOCK: BUS['event'] = 'action:' + str(value.get('action'))[:80]
            self.sendb(200, _json_bytes({'ok': True, 'result': result})); return
        self.sendb(404, b'{"ok":false,"error":"not_found"}')


def main():
    global TOKEN, TOPIC
    ap = argparse.ArgumentParser(); ap.add_argument('--token', required=True); ap.add_argument('--topic', default=''); args = ap.parse_args()
    TOKEN = args.token.strip(); TOPIC = args.topic.strip()
    if len(TOKEN) < 24: raise SystemExit('token too short')
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    set_title('ARCHIE PHONE starting'); signal('phone bridge starting', f'127.0.0.1:{PORT}')
    threading.Thread(target=tunnel_worker, name='archie-phone-tunnel', daemon=True).start()
    threading.Thread(target=auto_commit_worker, name='archie-phone-text', daemon=True).start()
    print(f'ARCHIE PHONE local http://127.0.0.1:{PORT}', flush=True)
    try: server.serve_forever(poll_interval=.12)
    finally:
        STOP.set(); server.server_close()


if __name__ == '__main__': main()
