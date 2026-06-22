#!/usr/bin/env python3
"""
Tabbit Chat Proxy v3 — Webpack Module Injection
=================================================
通过 CDP 调用 Tabbit 内部 webpack 模块，动态发现 + 自动导航。
参考: hwttop5/tabbit2api PR#2 更新的模块 ID。

启动:  python3 tabbit_proxy_v3.py
"""

import json, sys, time, uuid, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import websocket

CDP_URL = "http://127.0.0.1:9222"
HOST = "127.0.0.1"
PORT = 9090
TIMEOUT_S = 180
CHAT_URL = "https://web.tabbit.ai/chat/new"

MODELS = {
    "tabbit/priority": "priority",
    "tabbit/default": "Default",
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

PRIORITY_CHAIN = [
    "Claude-Opus-4.7", "GPT-5.5", "Claude-Sonnet-4.6",
    "GPT-5.4", "DeepSeek-V4-Pro", "GLM-5.1", "Gemini-3.1-Pro",
]

# ─── CDP Connection ──────────────────────────────────────────────────────────

_ws = None
_msg_id = 0
_msg_lock = threading.Lock()
_response_map = {}
_response_map_lock = threading.Lock()


def cdp_send(method, params=None, timeout=30):
    global _msg_id
    with _msg_lock:
        _msg_id += 1
        mid = _msg_id
    msg = {"id": mid, "method": method}
    if params:
        msg["params"] = params
    _ws.send(json.dumps(msg))
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _response_map_lock:
            if mid in _response_map:
                resp = _response_map.pop(mid)
                if "error" in resp:
                    raise RuntimeError(f"CDP error: {resp['error']}")
                return resp.get("result", {})
        time.sleep(0.05)
    raise TimeoutError(f"CDP timeout for {method}")


def _ws_reader():
    global _ws
    while True:
        try:
            raw = _ws.recv()
            msg = json.loads(raw)
            mid = msg.get("id")
            if mid is not None:
                with _response_map_lock:
                    _response_map[mid] = msg
        except Exception as e:
            print(f"[cdp] reader error: {e}", flush=True)
            break


def get_tabbit_page():
    """Find a Tabbit page, preferring chat/session pages."""
    import urllib.request
    resp = urllib.request.urlopen(f"{CDP_URL}/json")
    targets = json.loads(resp.read())
    tabbit = [t for t in targets if "tabbit" in t.get("url", "").lower()
              and t.get("type") == "page"]
    if not tabbit:
        return None
    # Prefer /chat/ or /session/ pages
    chat_pages = [t for t in tabbit if "/chat/" in t.get("url", "")]
    session_pages = [t for t in tabbit if "/session/" in t.get("url", "")]
    candidates = chat_pages or session_pages or tabbit
    return candidates[0]["id"]


def cdp_connect():
    global _ws
    page_id = get_tabbit_page()
    if not page_id:
        raise RuntimeError("No Tabbit page found")
    _ws = websocket.create_connection(
        f"ws://127.0.0.1:9222/devtools/page/{page_id}", timeout=300)
    t = threading.Thread(target=_ws_reader, daemon=True)
    t.start()
    cdp_send("Runtime.enable")
    cdp_send("Page.enable")
    print(f"[cdp] Connected to {page_id[:8]}", flush=True)
    return page_id


def ensure_chat_page():
    """Navigate to chat page if not already there."""
    result = cdp_send("Runtime.evaluate", {
        "expression": "location.href",
        "returnByValue": True,
    })
    url = result.get("result", {}).get("value", "")
    if "/chat/" in url or "/session/" in url:
        print(f"[cdp] Already on chat page: {url[:60]}", flush=True)
        return

    print(f"[cdp] Navigating to chat page...", flush=True)
    cdp_send("Page.navigate", {"url": CHAT_URL})
    # Wait for page load
    time.sleep(5)
    # Wait for webpack to be ready
    for _ in range(10):
        try:
            r = cdp_send("Runtime.evaluate", {
                "expression": "!!window.webpackChunk_N_E",
                "returnByValue": True,
            }, timeout=5)
            if r.get("result", {}).get("value"):
                print("[cdp] Webpack ready", flush=True)
                return
        except:
            pass
        time.sleep(1)
    print("[cdp] Warning: webpack may not be ready", flush=True)


# ─── Dynamic Webpack Module Discovery ────────────────────────────────────────

DISCOVER_JS = r"""
(() => {
    let r = null;
    self.webpackChunk_N_E.push([[Symbol("discover")], {}, (req) => { r = req; }]);
    if (!r) return JSON.stringify({error: "no_runtime"});

    const result = {};

    // Find sendMessage: function with setMessages + onChatFinish in signature
    for (let id = 0; id < 100000; id++) {
        if (result.sendMessage) break;
        try {
            const mod = r(id);
            if (!mod) continue;
            for (const k of Object.keys(mod)) {
                if (typeof mod[k] !== 'function') continue;
                const src = mod[k].toString().slice(0, 1000);
                if (src.includes('setMessages') && src.includes('onChatFinish')) {
                    result.sendMessage = {id, key: k};
                    break;
                }
            }
        } catch {}
    }

    // Find modes: object with ASK property
    for (let id = 0; id < 100000; id++) {
        if (result.modes) break;
        try {
            const mod = r(id);
            if (!mod) continue;
            for (const k of Object.keys(mod)) {
                if (mod[k] && typeof mod[k] === 'object' && mod[k].ASK !== undefined) {
                    result.modes = {id, key: k};
                    break;
                }
            }
        } catch {}
    }

    return JSON.stringify(result);
})()
"""


def discover_modules():
    print("[discover] Scanning webpack modules...", flush=True)
    t0 = time.time()
    try:
        result = cdp_send("Runtime.evaluate", {
            "expression": DISCOVER_JS,
            "returnByValue": True,
            "timeout": 30000,
        }, timeout=35)
    except Exception as e:
        print(f"[discover] Error: {e}", flush=True)
        return None

    val = result.get("result", {}).get("value")
    if not val:
        return None

    modules = json.loads(val)
    elapsed = time.time() - t0

    sm = modules.get("sendMessage")
    md = modules.get("modes")
    if sm:
        print(f"[discover] sendMessage: runtime({sm['id']}).{sm['key']} ({elapsed:.1f}s)", flush=True)
    else:
        print(f"[discover] sendMessage NOT FOUND ({elapsed:.1f}s)", flush=True)
    if md:
        print(f"[discover] modes: runtime({md['id']}).{md['key']}", flush=True)
    else:
        print(f"[discover] modes NOT FOUND", flush=True)

    return modules if sm and md else None


# ─── Chat via Webpack ───────────────────────────────────────────────────────

def make_chat_js(sm_id, sm_key, md_id, md_key):
    return rf"""
(async () => {{
    let __r = null;
    self.webpackChunk_N_E.push([[Symbol("chat")], {{}}, (req) => {{ __r = req; }}]);
    if (!__r) return JSON.stringify({{ok:false, error:"no_runtime"}});

    const sendMessage = __r({sm_id}).{sm_key};
    const modes = __r({md_id}).{md_key};
    if (typeof sendMessage !== 'function') return JSON.stringify({{ok:false, error:"no_sendMessage"}});
    if (!modes?.ASK) return JSON.stringify({{ok:false, error:"no_modes_ASK"}});

    const prompt = $PROMPT;
    const selectedModel = $MODEL;
    const timeoutMs = $TIMEOUT;

    let settled = false, resultPayload = null;

    function findAssistant(msgs) {{
        for (let i = msgs.length - 1; i >= 0; i--)
            if (msgs[i]?.type === "assistant") return msgs[i];
        return null;
    }}

    function collectText(node) {{
        if (!node) return "";
        const parts = [];
        function visit(n) {{
            if (!n) return;
            if (Array.isArray(n)) {{ n.forEach(visit); return; }}
            if (typeof n === "string") {{ parts.push(n); return; }}
            if (typeof n !== "object") return;
            if (n.type === "assistant" && typeof n.content === "string") parts.push(n.content);
            if (Array.isArray(n.messages)) visit(n.messages);
            if (Array.isArray(n.content)) visit(n.content);
        }}
        visit(node.messages || []);
        return parts.join("").trim();
    }}

    function settle(p) {{ if (!settled) {{ settled = true; resultPayload = p; }} }}

    function check() {{
        const a = findAssistant(state.messages);
        if (!a || a.generating) return false;
        if (a.messages?.some(e => e?.type === "login")) {{ settle({{ok:false, error:"login_required"}}); return true; }}
        const errs = (a.messages||[]).filter(e=>e?.type==="error").map(e=>(e.code?"["+e.code+"] ":"")+(e.content||e.message||""));
        if (errs.length) {{ settle({{ok:false, error:"tabbit_error", detail:errs.join("\\n")}}); return true; }}
        const t = collectText(a);
        if (t) {{ settle({{ok:true, text:t}}); return true; }}
        return false;
    }}

    const state = {{messages:[]}};
    const setMessages = (_s, u) => {{ state.messages = typeof u==="function"?u(state.messages):u; check(); }};
    setTimeout(() => settle({{ok:false, error:"timeout"}}), timeoutMs);

    try {{
        await sendMessage({{
            messageId: null, message: prompt, originHTML: "", references: [],
            sessionId: "", model: selectedModel, selectedModels: [selectedModel],
            mod: modes.ASK, url: "", source: "singleSession", useDirectApi: false,
            models: [], updateSessionId:()=>{{}}, setMessages,
            setSessionTitle:()=>{{}}, shouldApplyAutoSessionTitle:()=>true,
            onBeforeSend:()=>{{}}, startGenerating:()=>{{}},
            stopGenerating:()=>{{ setTimeout(()=>settle({{ok:false, error:"stopGenerating"}}), 100); }},
            associateTabWithSession:()=>{{}}, updateBrowserUseStatus:()=>{{}},
            errorMessages:{{}}, onModelChange:()=>{{}}, refreshModels:()=>{{}},
            onChatFinish:()=>{{ setTimeout(()=>settle({{ok:false, error:"chatFinished"}}), 100); }},
            onFailed:(...a)=>{{ setTimeout(()=>settle({{ok:false, error:"send_failed", detail:a.map(String).join(" | ")}}), 100); }},
        }});
    }} catch(e) {{ settle({{ok:false, error:"send_threw", detail:String(e)}}); }}

    while (!resultPayload) await new Promise(r=>setTimeout(r,200));

    if (resultPayload.ok) {{
        const a = findAssistant(state.messages);
        const t = collectText(a);
        if (t) resultPayload.text = t;
    }}
    return JSON.stringify(resultPayload);
}})()
"""


_module_info = None


def send_chat(prompt, model, timeout_s=TIMEOUT_S):
    global _module_info
    if not _module_info:
        return {"ok": False, "error": "modules_not_discovered"}

    sm = _module_info["sendMessage"]
    md = _module_info["modes"]
    js = make_chat_js(sm["id"], sm["key"], md["id"], md["key"])
    js = js.replace("$PROMPT", json.dumps(prompt)) \
           .replace("$MODEL", json.dumps(model)) \
           .replace("$TIMEOUT", str(int(timeout_s * 1000)))

    try:
        result = cdp_send("Runtime.evaluate", {
            "expression": js, "awaitPromise": True,
            "returnByValue": True, "timeout": (timeout_s + 10) * 1000,
        }, timeout=timeout_s + 15)
    except Exception as e:
        return {"ok": False, "error": "cdp_error", "detail": str(e)}

    val = result.get("result", {}).get("value")
    if not val:
        return {"ok": False, "error": "empty_result"}
    try:
        return json.loads(val)
    except:
        return {"ok": False, "error": "parse_error", "detail": str(val)[:500]}


# ─── HTTP Server ─────────────────────────────────────────────────────────────

def sse_chunk(text, model="tabbit", stop=False):
    d = {"id": f"chatcmpl-{uuid.uuid4().hex[:8]}", "object": "chat.completion.chunk",
         "created": int(time.time()), "model": model,
         "choices": [{"index": 0, "delta": {} if stop else {"content": text},
                      "finish_reason": "stop" if stop else None}]}
    return f"data: {json.dumps(d)}\n\n"


def build_content(messages):
    parts = []
    for m in messages:
        role, content = m.get("role", ""), m.get("content", "")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
        if role == "system": parts.append(f"[System]\n{content}")
        elif role == "user": parts.append(content)
        elif role == "assistant": parts.append(f"[Assistant]\n{content}")
    return "\n\n".join(parts)


def resolve_model(requested):
    key = requested.lower().strip()
    if key in ("default", "tabbit/priority", ""): return PRIORITY_CHAIN[0]
    if key in MODELS: return MODELS[key]
    for k, v in MODELS.items():
        if v.lower() == key: return v
    return requested


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code); self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self._json({"status": "ok", "modules": bool(_module_info)})
        elif self.path == "/v1/models":
            self._json({"object": "list", "data": [
                {"id": k, "object": "model", "owned_by": "tabbit"} for k in MODELS
            ]})
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_response(404); self.end_headers(); return
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            req = json.loads(body)
        except:
            self._json({"error": "bad request"}, 400); return

        messages = req.get("messages", [])
        if not messages:
            self._json({"error": "no messages"}, 400); return

        content = build_content(messages)
        model = resolve_model(req.get("model", "default"))
        stream = req.get("stream", False)
        print(f"[chat] model={model} stream={stream} msg={content[:80]}...", flush=True)

        models_to_try = [model]
        if model in PRIORITY_CHAIN:
            models_to_try = PRIORITY_CHAIN[PRIORITY_CHAIN.index(model):]

        last_error = None
        for try_model in models_to_try:
            t0 = time.time()
            result = send_chat(content, try_model)
            elapsed = time.time() - t0

            if result.get("ok") and result.get("text"):
                print(f"[chat] OK ({try_model}, {elapsed:.1f}s, {len(result['text'])}c)", flush=True)
                if stream: self._stream(result["text"], try_model)
                else: self._full(result["text"], try_model)
                return
            last_error = result.get("detail", result.get("error", "unknown"))
            print(f"[chat] {try_model} failed: {str(last_error)[:80]}", flush=True)
            if result.get("error") == "login_required": break

        self._json({"error": f"All failed: {last_error}"}, 500)

    def _full(self, text, model):
        self._json({"id": f"chatcmpl-{uuid.uuid4().hex[:8]}", "object": "chat.completion",
                     "created": int(time.time()), "model": model,
                     "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                  "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})

    def _stream(self, text, model):
        self.send_response(200); self._cors()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache"); self.end_headers()
        for i in range(0, len(text), 20):
            self.wfile.write(sse_chunk(text[i:i+20], model=model).encode()); self.wfile.flush()
        self.wfile.write(sse_chunk("", model=model, stop=True).encode())
        self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()

    def log_message(self, format, *args): pass


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    global _module_info
    print("=" * 50, flush=True)
    print("  Tabbit Proxy v3 (Webpack Injection)", flush=True)
    print("=" * 50, flush=True)

    try:
        cdp_connect()
    except Exception as e:
        print(f"[error] CDP: {e}", flush=True); sys.exit(1)

    ensure_chat_page()
    _module_info = discover_modules()
    if not _module_info:
        print("[error] Module discovery failed", flush=True); sys.exit(1)

    print("[test] Quick test...", flush=True)
    r = send_chat("say ok", "GPT-5.5", timeout_s=30)
    if r.get("ok"):
        print(f"[test] OK: {r.get('text', '')[:50]}", flush=True)
    else:
        print(f"[test] Failed: {r.get('error')}: {r.get('detail', '')[:80]}", flush=True)

    server = ThreadedHTTPServer((HOST, PORT), Handler)
    print(f"\n[proxy] http://{HOST}:{PORT}", flush=True)
    print(f"[proxy] Models: {len(MODELS)}\n", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...", flush=True); server.shutdown()


if __name__ == "__main__":
    main()
