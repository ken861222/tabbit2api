#!/usr/bin/env python3
"""
Tabbit Chat Reverse Proxy v2 - OpenAI Compatible API
=====================================================
通过 CDP 控制 Tabbit 浏览器，网络层用 JS 拦截器捕获响应。
支持页面导航（新对话自动跳转 session URL）。

启动:  python3 tabbit_proxy_v2.py
"""

import json, sys, time, uuid, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import websocket

CDP_URL = "http://127.0.0.1:9222"
HOST = "127.0.0.1"
PORT = 9090

MODELS = {
    "default free": "Default Free",
    "gpt-5.5": "GPT-5.5", "gpt-5.4": "GPT-5.4", "gpt-5.2-chat": "GPT-5.2-Chat",
    "claude-opus-4.8": "Claude-Opus-4.8", "claude-opus-4.7": "Claude-Opus-4.7",
    "claude-sonnet-4.6": "Claude-Sonnet-4.6", "claude-haiku-4.5": "Claude-Haiku-4.5",
    "deepseek-v4-pro": "DeepSeek-V4-Pro", "deepseek-v4-flash": "DeepSeek-V4-Flash",
    "deepseek-v3.2": "DeepSeek-V3.2",
    "gemini-3.5-flash": "Gemini-3.5-Flash", "gemini-3.1-pro": "Gemini-3.1-Pro",
    "minimax-m3": "MiniMax-M3", "minimax-m2.7": "MiniMax-M2.7",
    "kimi-k2.6": "Kimi-K2.6", "kimi-k2.5": "Kimi-K2.5",
    "glm-5.1": "GLM-5.1", "qwen3.5-plus": "Qwen3.5-Plus",
    "doubao-seed-1.8": "Doubao-Seed-1.8",
}

_ws = None
_msg_id = 0
_msg_lock = threading.Lock()
_response_map = {}
_response_map_lock = threading.Lock()
_request_lock = threading.Lock()
_reconnecting = False
_page_load_event = threading.Event()


# ---------------------------------------------------------------------------
# Fetch interceptor JS — uses resp.text() for reliability
# ---------------------------------------------------------------------------

FETCH_INTERCEPTOR_JS = r"""
(function() {
    if (window.__proxyV2) return 'already';
    const origFetch = window.fetch;
    window.__chatResp = null;
    window.__chatDone = false;
    window.__chatErr = null;
    window.__chatStatus = 0;

    window.fetch = async function(...args) {
        const resp = await origFetch.apply(this, args);
        try {
            const url0 = args[0];
            const urlStr = (typeof url0 === 'string') ? url0 : (url0 && url0.url ? url0.url : '');
            if (urlStr.includes('chat/completion')) {
                window.__chatStatus = resp.status;
                try {
                    const text = await resp.text();
                    if (resp.ok) {
                        window.__chatResp = text;
                    } else {
                        window.__chatErr = text || ('HTTP ' + resp.status);
                    }
                    window.__chatDone = true;
                } catch(e) {
                    window.__chatErr = e.message;
                    window.__chatDone = true;
                }
            }
        } catch(e) {}
        return resp;
    };
    window.__proxyV2 = true;
    return 'ok';
})()
"""


# ---------------------------------------------------------------------------
# CDP connection
# ---------------------------------------------------------------------------

def get_tabbit_page_id():
    import urllib.request
    try:
        resp = urllib.request.urlopen(f"{CDP_URL}/json")
        targets = json.loads(resp.read())
        # Prefer session pages > newtab > any tabbit page
        tabbit = [t for t in targets if "web.tabbit.ai" in t.get("url", "")]
        session_pages = [t for t in tabbit if "/session/" in t.get("url", "")]
        newtab_pages = [t for t in tabbit if "/newtab" in t.get("url", "")]
        candidates = session_pages or newtab_pages or tabbit
        for t in candidates:
            tid = t["id"]
            try:
                ws = websocket.create_connection(f"ws://127.0.0.1:9222/devtools/page/{tid}", timeout=3)
                ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
                    "expression": "!!document.querySelector('[contenteditable]')", "returnByValue": True
                }}))
                r = json.loads(ws.recv())
                ws.close()
                if r.get("result", {}).get("result", {}).get("value"):
                    print(f"[cdp] Found editor on {tid[:8]}", flush=True)
                    return tid
            except:
                pass
        if candidates:
            return candidates[0]["id"]
    except Exception as e:
        print(f"[error] get page id: {e}", flush=True)
    return None


def _ws_reader():
    global _ws
    while True:
        try:
            raw = _ws.recv()
            msg = json.loads(raw)
            mid = msg.get("id")
            method = msg.get("method")
            if method == "Page.loadEventFired":
                _page_load_event.set()
            elif mid is not None:
                with _response_map_lock:
                    if mid in _response_map:
                        _response_map[mid] = msg
        except Exception as e:
            print(f"[cdp] reader error: {e}", flush=True)
            break
    cdp_reconnect()


def cdp_init():
    global _ws
    page_id = get_tabbit_page_id()
    if not page_id:
        print("[error] No Tabbit panel page found", flush=True)
        return False

    uri = f"ws://127.0.0.1:9222/devtools/page/{page_id}"
    _ws = websocket.create_connection(uri, timeout=300, max_size=50*1024*1024, ping_interval=30, ping_timeout=10)
    print(f"[cdp] Connected to {page_id[:8]}", flush=True)

    threading.Thread(target=_ws_reader, daemon=True).start()

    # Enable Page.loadEventFired for navigation detection
    cdp_send("Page.enable", timeout=5)

    # Register auto-injection for new page contexts (survives navigation)
    cdp_send("Page.addScriptToEvaluateOnNewDocument", {"source": FETCH_INTERCEPTOR_JS}, timeout=5)

    # Inject into current page
    r = cdp_eval(FETCH_INTERCEPTOR_JS)
    print(f"[cdp] Interceptor: {r}", flush=True)
    return True


def cdp_reconnect():
    global _ws, _reconnecting
    if _reconnecting:
        return
    _reconnecting = True
    print("[cdp] Reconnecting...", flush=True)
    for attempt in range(5):
        try:
            time.sleep(2 ** attempt)
            try: _ws.close()
            except: pass
            if not cdp_init():
                continue
            print(f"[cdp] Reconnected on attempt {attempt+1}", flush=True)
            _reconnecting = False
            return
        except Exception as e:
            print(f"[cdp] Reconnect error: {e}", flush=True)
    print("[cdp] Reconnect FAILED", flush=True)
    _reconnecting = False


def cdp_send(method, params=None, timeout=30):
    global _msg_id
    with _msg_lock:
        _msg_id += 1
        mid = _msg_id
    with _response_map_lock:
        _response_map[mid] = None
    try:
        _ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    except Exception:
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _response_map_lock:
            val = _response_map.get(mid)
            if val is not None:
                del _response_map[mid]
                return val
        time.sleep(0.05)
    with _response_map_lock:
        _response_map.pop(mid, None)
    return None


def cdp_eval(expression, timeout=30):
    result = cdp_send("Runtime.evaluate", {
        "expression": expression, "awaitPromise": True, "returnByValue": True
    }, timeout=timeout)
    if result:
        return result.get("result", {}).get("result", {}).get("value")
    return None


# ---------------------------------------------------------------------------
# Dynamic user ID
# ---------------------------------------------------------------------------

_cached_user_id = None

def get_user_id():
    global _cached_user_id
    if _cached_user_id:
        return _cached_user_id
    uid = cdp_eval("""
    (function() {
        try {
            const raw = localStorage.getItem('user_info') || localStorage.getItem('userInfo');
            if (raw) { const o = JSON.parse(raw); return o.user_id || o.userId || null; }
        } catch(e) {}
        return null;
    })()
    """)
    _cached_user_id = uid or "50bd8287-2f72-4d29-ad4e-4d51b44849bf"
    print(f"[cdp] User ID: {_cached_user_id[:12]}...", flush=True)
    return _cached_user_id


# ---------------------------------------------------------------------------
# Message assembly
# ---------------------------------------------------------------------------

def _build_content(messages):
    MAX_LEN = 19000  # Tabbit editor char limit ~20000
    system_msgs = [m for m in messages if m.get("role") == "system"]
    user_msgs = [m for m in messages if m.get("role") == "user"]
    parts = []
    if system_msgs:
        sys_text = "\n".join(m.get("content", "") for m in system_msgs)
        parts.append(f"[System Instructions]\n{sys_text}\n[/System Instructions]")
    if user_msgs:
        parts.append("\n".join(m.get("content", "") for m in user_msgs))
    text = "\n\n".join(parts)
    if len(text) > MAX_LEN:
        user_text = "\n\n".join(m.get("content", "") for m in user_msgs)
        budget = MAX_LEN - len(user_text) - 200
        if budget > 500 and system_msgs:
            sys_text = "\n".join(m.get("content", "") for m in system_msgs)
            text = f"[System Instructions]\n{sys_text[:budget]}\n...[truncated]\n[/System Instructions]\n\n{user_text}"
        else:
            text = text[:MAX_LEN]
        print(f"[build] truncated to {len(text)} chars", flush=True)
    return text


# ---------------------------------------------------------------------------
# Message sending
# ---------------------------------------------------------------------------

JS_SEND = r"""
(function() {
    const editor = document.querySelector('[contenteditable]');
    if (!editor) return 'no editor';
    editor.focus();
    editor.textContent = __MSG__;
    editor.dispatchEvent(new Event('input', { bubbles: true }));
    setTimeout(() => {
        const btns = Array.from(document.querySelectorAll('button'));
        const sendBtn = btns.find(b => {
            const p = b.querySelector('svg path');
            return p && p.getAttribute('d')?.includes('7-7 7 7');
        });
        if (sendBtn) sendBtn.dispatchEvent(new MouseEvent('click', {bubbles:true,cancelable:true}));
    }, 300);
    return 'sent';
})()
"""


def send_message(content, model="GPT-5.5"):
    """Send message, wait for page navigation if needed, poll for response."""
    _page_load_event.clear()

    # Reset interceptor state on current page
    cdp_eval("window.__chatResp = null; window.__chatDone = false; window.__chatErr = null; window.__chatStatus = 0; 'ok'", timeout=5)

    escaped = json.dumps(content)
    js = JS_SEND.replace("__MSG__", escaped)
    result = cdp_eval(js, timeout=5)
    print(f"[send] trigger: {result}", flush=True)
    if not result or 'no' in str(result):
        return {"error": f"send failed: {result}"}

    # Wait for potential page navigation (first msg → session URL)
    navigated = _page_load_event.wait(timeout=12)
    if navigated:
        print("[send] page navigated", flush=True)
        time.sleep(1)  # let new page settle

    # Ensure interceptor is installed (addScriptToEvaluateOnNewDocument may have missed)
    has_interceptor = cdp_eval("!!window.__proxyV2", timeout=5)
    if not has_interceptor:
        print("[send] interceptor missing, re-injecting...", flush=True)
        cdp_eval(FETCH_INTERCEPTOR_JS, timeout=5)

    # Poll for response (addScriptToEvaluateOnNewDocument re-installs interceptor)
    for i in range(240):
        time.sleep(0.5)
        done = cdp_eval("!!window.__chatDone", timeout=5)
        if done:
            resp = cdp_eval("window.__chatResp || null", timeout=5)
            err = cdp_eval("window.__chatErr || null", timeout=5)
            if resp:
                return {"status": 200, "body": resp}
            if err:
                return {"error": err}
            break
        if i > 0 and i % 20 == 0:
            print(f"[send] waiting... ({i//2}s)", flush=True)

    return {"error": "timeout"}


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def _extract_sse_fragments(sse_text):
    for line in sse_text.split("\n"):
        if line.startswith("data:"):
            d = line[5:].strip()
            if not d or d == "{}":
                continue
            try:
                obj = json.loads(d)
                c = obj.get("content", "")
                if c:
                    yield c
            except:
                pass


def parse_sse_content(sse_text):
    return "".join(_extract_sse_fragments(sse_text))


def sse_chunk(content, model="tabbit", stop=False):
    return json.dumps({
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {"content": content} if content else {}, "finish_reason": "stop" if stop else None}],
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Coupon management
# ---------------------------------------------------------------------------

def get_quota():
    result = cdp_eval("""
    (function() {
        try {
            const els = document.querySelectorAll('*');
            const data = {};
            for (const el of els) {
                const t = el.textContent;
                if (!t || el.children.length > 0) continue;
                if (!data.plan && (t === 'Pro' || t === 'free' || t === 'Free')) data.plan = t;
                if (!data.remaining && /^\\d+\\.\\d+\\s*%$/.test(t)) data.remaining = t;
                if (!data.reset && t.includes('小时后重置')) data.reset = t;
            }
            return JSON.stringify(data);
        } catch(e) { return JSON.stringify({error: e.message}); }
    })()
    """)
    if result:
        try: return json.loads(result)
        except: return {"raw": result}
    return {"error": "failed"}


def claim_coupon():
    uid = get_user_id()
    result = cdp_eval(f"""
    (async function() {{
        try {{
            const resp = await fetch('/api/commerce/activity/v1/participate', {{
                method: 'POST', headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{user_id: '{uid}', request_no: 'claim_' + Date.now()}})
            }});
            return await resp.text();
        }} catch(e) {{ return JSON.stringify({{error: e.message}}); }}
    }})()
    """, timeout=10)
    if result:
        try: return json.loads(result)
        except: return {"raw": result}
    return {"error": "claim failed"}


def list_coupons():
    uid = get_user_id()
    result = cdp_eval(f"""
    (async function() {{
        try {{
            const resp = await fetch('/api/commerce/benefit/v1/coupon/list?user_id={uid}&coupon_type=weekly_reset_coupon&offset=0&limit=20&user_coupon_status=1');
            return await resp.text();
        }} catch(e) {{ return JSON.stringify({{error: e.message}}); }}
    }})()
    """, timeout=10)
    if result:
        try: return json.loads(result)
        except: return {"raw": result}
    return {"error": "list failed"}


def use_coupon(coupon_code):
    uid = get_user_id()
    result = cdp_eval(f"""
    (async function() {{
        try {{
            const resp = await fetch('/api/commerce/benefit/v1/coupon/use', {{
                method: 'POST', headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{user_id:'{uid}',coupon_code:'{coupon_code}',coupon_type:'weekly_reset_coupon',request_no:'use_'+Date.now()}})
            }});
            return await resp.text();
        }} catch(e) {{ return JSON.stringify({{error: e.message}}); }}
    }})()
    """, timeout=10)
    if result:
        try: return json.loads(result)
        except: return {"raw": result}
    return {"error": "use failed"}


def claim_and_use_coupon():
    cr = claim_coupon()
    print(f"[coupon] claim: {cr}", flush=True)
    if cr.get("participation_result") != "success":
        return {"error": "claim failed", "detail": cr}
    cl = list_coupons()
    print(f"[coupon] list: {json.dumps(cl)[:200]}", flush=True)
    cc = (cl.get("coupons") or [{}])[0].get("coupon_code")
    if not cc:
        return {"error": "no coupon code found"}
    ur = use_coupon(cc)
    print(f"[coupon] use: {ur}", flush=True)
    return {"claimed": cr, "coupon_code": cc, "use_result": ur}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _json_response(self, data, status=200):
        self.send_response(status); self._cors()
        self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def do_OPTIONS(self):
        self.send_response(200); self._cors(); self.end_headers()

    def do_GET(self):
        if self.path == "/v1/coupons":
            self._json_response(list_coupons())
        elif self.path == "/v1/usage":
            self._json_response(get_quota())
        elif self.path == "/v1/models":
            self._json_response({"object": "list", "data": [{"id": k, "object": "model", "owned_by": "tabbit"} for k in MODELS]})
        elif self.path == "/health":
            self.send_response(200); self._cors()
            self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == "/v1/coupon/claim":
            self._json_response(claim_coupon()); return
        elif self.path == "/v1/coupon/use":
            try:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                req = json.loads(body)
                code = req.get("coupon_code")
                result = use_coupon(code) if code else claim_and_use_coupon()
            except:
                result = claim_and_use_coupon()
            self._json_response(result); return
        elif self.path != "/v1/chat/completions":
            self.send_response(404); self.end_headers(); return

        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            req = json.loads(body)
        except:
            self.send_response(400); self.end_headers(); return

        messages = req.get("messages", [])
        if not messages:
            self._json_response({"error": "no messages"}, 400); return

        content = _build_content(messages)
        model = MODELS.get(req.get("model", "default free").lower(), req.get("model", "default free"))
        stream = req.get("stream", False)
        print(f"[chat] model={model} stream={stream} msg={content[:80]}", flush=True)

        try:
            with _request_lock:
                if stream:
                    self._handle_stream(content, model)
                else:
                    self._handle_full(content, model)
        except Exception as e:
            import traceback; traceback.print_exc()
            self._json_response({"error": str(e)}, 500)

    def _handle_full(self, content, model):
        result = send_message(content, model)
        if "error" in result:
            err = str(result["error"])
            if "用量已用完" in err or "已用完" in err or "492" in err:
                print("[chat] quota exhausted, auto claiming...", flush=True)
                cr = claim_and_use_coupon()
                if cr.get("use_result", {}).get("success"):
                    result = send_message(content, model)
                    if "error" not in result:
                        pass
                    else:
                        self._json_response(result, 500); return
                else:
                    self._json_response({"error": "quota exhausted", "coupon": cr}, 500); return
            else:
                self._json_response(result, 500); return

        text = parse_sse_content(result.get("body", ""))
        if not text:
            self._json_response({"error": "empty response"}, 500); return

        print(f"[chat] OK: {text[:80]}", flush=True)
        self._json_response({
            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    def _handle_stream(self, content, model):
        self.send_response(200); self._cors()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache"); self.end_headers()

        result = send_message(content, model)
        if "error" in result:
            self.wfile.write(f"data: {json.dumps({'error': result['error']})}\n\n".encode())
            self.wfile.flush()
            return

        body = result.get("body", "")
        full_text = ""
        for frag in _extract_sse_fragments(body):
            full_text += frag
            self.wfile.write(f"data: {sse_chunk(frag, model=model)}\n\n".encode())
            self.wfile.flush()

        self.wfile.write(f"data: {sse_chunk('', model=model, stop=True)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        if full_text:
            print(f"[chat] streamed {len(full_text)} chars", flush=True)

    def log_message(self, format, *args):
        pass


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    print("=" * 50, flush=True)
    print("  Tabbit Reverse Proxy v2", flush=True)
    print("=" * 50, flush=True)

    if not cdp_init():
        print("[error] Cannot connect to Tabbit", flush=True)
        sys.exit(1)

    get_user_id()
    server = ThreadedHTTPServer((HOST, PORT), Handler)
    print(f"\n[proxy] http://{HOST}:{PORT}", flush=True)
    print(f"[proxy] POST http://{HOST}:{PORT}/v1/chat/completions", flush=True)
    print(f"[proxy] Models: {', '.join(MODELS.keys())}\n", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...", flush=True)
        server.shutdown()


if __name__ == "__main__":
    main()
