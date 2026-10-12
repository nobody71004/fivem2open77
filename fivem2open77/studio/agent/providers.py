"""Chat model providers for the Studio's agent: Claude (Anthropic Messages API) and Ollama (local
models), behind one small interface, plus a scripted provider for tests.

Conversation format used by the agent (provider-neutral):
    {"role": "user", "content": [{"type": "text", "text": ...}, {"type": "image", "png": b64}]}
    {"role": "assistant", "content": [{"type": "text", "text": ...},
                                      {"type": "tool_call", "id": ..., "name": ..., "input": {...}}]}
    {"role": "tool", "results": [{"id": ..., "name": ..., "content": [text/image blocks], "is_error": bool}]}
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
import uuid


class ProviderError(RuntimeError):
    pass


class Reply:
    def __init__(self, blocks, stop_reason=None, usage=None):
        self.blocks = blocks                 # neutral assistant content blocks
        self.stop_reason = stop_reason
        self.usage = usage or {}

    @property
    def text(self):
        return "\n".join(b["text"] for b in self.blocks if b["type"] == "text").strip()

    @property
    def tool_calls(self):
        return [b for b in self.blocks if b["type"] == "tool_call"]


def _http(url, body=None, headers=None, timeout=300, method=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:800]
        try:
            j = json.loads(detail)
            detail = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else (j.get("error") or detail)
        except ValueError:
            pass
        raise ProviderError(f"HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise ProviderError(f"cannot reach {url.split('/')[2]}: {getattr(e, 'reason', e)}") from e


# ----------------------------------------------------------------------------- Claude
class AnthropicProvider:
    name = "anthropic"
    VERSION = "2023-06-01"

    def __init__(self, api_key: str, model: str = "", base_url="https://api.anthropic.com", max_tokens=4096):
        if not api_key:
            raise ProviderError("no Claude API key: add one in Settings > AI")
        self.key = api_key
        self.base = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.vision = True

    def _headers(self):
        return {"x-api-key": self.key, "anthropic-version": self.VERSION}

    def list_models(self) -> list:
        out = _http(f"{self.base}/v1/models?limit=100", headers=self._headers(), timeout=30)
        return [{"id": m.get("id"), "name": m.get("display_name") or m.get("id"), "created": m.get("created_at")}
                for m in out.get("data", [])]

    def pick_model(self) -> str:
        """The configured model, or the newest Sonnet (then any model) the key can use."""
        if self.model:
            return self.model
        models = self.list_models()
        for pref in ("sonnet", "opus", "haiku"):
            for m in models:
                if pref in (m["id"] or ""):
                    self.model = m["id"]
                    return self.model
        if models:
            self.model = models[0]["id"]
            return self.model
        raise ProviderError("the API key has no models available")

    @staticmethod
    def _content(blocks):
        out = []
        for b in blocks:
            if b["type"] == "text":
                if b["text"]:
                    out.append({"type": "text", "text": b["text"]})
            elif b["type"] == "image":
                out.append({"type": "image", "source": {"type": "base64", "media_type": b.get("mimeType", "image/png"), "data": b["png"]}})
        return out

    def _messages(self, messages):
        out = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": self._content(m["content"])})
            elif m["role"] == "assistant":
                c = []
                for b in m["content"]:
                    if b["type"] == "text" and b["text"]:
                        c.append({"type": "text", "text": b["text"]})
                    elif b["type"] == "tool_call":
                        c.append({"type": "tool_use", "id": b["id"], "name": b["name"], "input": b["input"]})
                out.append({"role": "assistant", "content": c or [{"type": "text", "text": "(no reply)"}]})
            elif m["role"] == "tool":
                out.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r["id"], "content": self._content(r["content"]) or
                     [{"type": "text", "text": "(empty)"}], "is_error": bool(r.get("is_error"))} for r in m["results"]]})
        # the API wants alternating roles: merge consecutive user turns
        merged = []
        for m in out:
            if merged and merged[-1]["role"] == m["role"] == "user":
                merged[-1]["content"] = merged[-1]["content"] + m["content"]
            else:
                merged.append(m)
        return merged

    def chat(self, system: str, messages: list, tools: list) -> Reply:
        model = self.pick_model()
        tdefs = [{"name": t["name"], "description": t["description"], "input_schema": t["schema"]} for t in tools]
        if tdefs:
            tdefs[-1] = dict(tdefs[-1], cache_control={"type": "ephemeral"})
        body = {"model": model, "max_tokens": self.max_tokens,
                "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                "messages": self._messages(messages), "tools": tdefs}
        out = _http(f"{self.base}/v1/messages", body, headers=self._headers(), timeout=600)
        blocks = []
        for c in out.get("content", []):
            if c.get("type") == "text":
                blocks.append({"type": "text", "text": c.get("text", "")})
            elif c.get("type") == "tool_use":
                blocks.append({"type": "tool_call", "id": c["id"], "name": c["name"], "input": c.get("input") or {}})
        return Reply(blocks, out.get("stop_reason"), out.get("usage"))


# ----------------------------------------------------------------------------- Ollama
class OllamaProvider:
    name = "ollama"

    def __init__(self, url="http://127.0.0.1:11434", model=""):
        self.url = url.rstrip("/")
        self.model = model
        self.vision = False       # set from the model's capabilities when known

    def list_models(self) -> list:
        out = _http(f"{self.url}/api/tags", timeout=15)
        return [{"id": m.get("name"), "name": m.get("name"), "size": m.get("size")} for m in out.get("models", [])]

    def pick_model(self) -> str:
        if self.model:
            return self.model
        models = self.list_models()
        if not models:
            raise ProviderError("Ollama has no models: run `ollama pull qwen2.5:7b` (or another tool-calling model)")
        self.model = models[0]["id"]
        return self.model

    def capabilities(self) -> list:
        try:
            out = _http(f"{self.url}/api/show", {"model": self.pick_model()}, timeout=15)
            return out.get("capabilities") or []
        except ProviderError:
            return []

    def _messages(self, system, messages):
        out = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                text = "\n".join(b["text"] for b in m["content"] if b["type"] == "text")
                imgs = [b["png"] for b in m["content"] if b["type"] == "image"]
                d = {"role": "user", "content": text}
                if imgs and self.vision:
                    d["images"] = imgs
                out.append(d)
            elif m["role"] == "assistant":
                text = "\n".join(b["text"] for b in m["content"] if b["type"] == "text")
                calls = [{"function": {"name": b["name"], "arguments": b["input"]}} for b in m["content"] if b["type"] == "tool_call"]
                d = {"role": "assistant", "content": text}
                if calls:
                    d["tool_calls"] = calls
                out.append(d)
            elif m["role"] == "tool":
                for r in m["results"]:
                    text = "\n".join(b["text"] for b in r["content"] if b["type"] == "text")
                    imgs = [b["png"] for b in r["content"] if b["type"] == "image"]
                    out.append({"role": "tool", "content": text or "(empty)", "tool_name": r["name"]})
                    if imgs and self.vision:
                        out.append({"role": "user", "content": f"(images returned by {r['name']})", "images": imgs})
        return out

    def chat(self, system: str, messages: list, tools: list) -> Reply:
        model = self.pick_model()
        if "vision" in self.capabilities():
            self.vision = True
        body = {"model": model, "stream": False, "messages": self._messages(system, messages),
                "tools": [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                             "parameters": t["schema"]}} for t in tools],
                "options": {"temperature": 0.2, "num_ctx": 32768}}
        out = _http(f"{self.url}/api/chat", body, timeout=900)
        msg = out.get("message") or {}
        blocks = []
        if msg.get("content"):
            blocks.append({"type": "text", "text": msg["content"]})
        for c in msg.get("tool_calls") or []:
            fn = c.get("function") or {}
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"_raw": args}
            blocks.append({"type": "tool_call", "id": "call_" + uuid.uuid4().hex[:10], "name": fn.get("name", ""),
                           "input": args})
        return Reply(blocks, out.get("done_reason"), {"input_tokens": out.get("prompt_eval_count"),
                                                      "output_tokens": out.get("eval_count")})


# ----------------------------------------------------------------------------- tests
class ScriptedProvider:
    """Plays back a list of replies (each a list of neutral blocks, or a callable(messages) -> blocks)."""
    name = "scripted"

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.vision = True
        self.model = "scripted"

    def list_models(self):
        return [{"id": "scripted", "name": "scripted"}]

    def chat(self, system, messages, tools):
        self.calls.append({"system": system, "messages": json.loads(json.dumps(messages)), "tools": [t["name"] for t in tools]})
        if not self.script:
            return Reply([{"type": "text", "text": "(script finished)"}], "end_turn")
        step = self.script.pop(0)
        blocks = step(messages) if callable(step) else step
        out = []
        for b in blocks:
            b = dict(b)
            if b["type"] == "tool_call" and "id" not in b:
                b["id"] = "call_" + uuid.uuid4().hex[:8]
            out.append(b)
        return Reply(out, "tool_use" if any(b["type"] == "tool_call" for b in out) else "end_turn")
