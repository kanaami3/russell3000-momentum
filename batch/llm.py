"""生成AIの呼び出し口。Gemini の無料枠を使う。

**既存スクリプトを書き換えずに済ませるため、Anthropic SDK と同じ形にしてある。**
呼び出し側は今まで通り

    resp = client.messages.create(model=..., max_tokens=..., messages=[...])
    text = "".join(b.text for b in resp.content if b.type == "text")
    resp.usage.input_tokens / resp.usage.output_tokens

と書ける。中で Gemini の REST API を叩いているだけ。差し替えは各スクリプトの
クライアント生成の1行だけで済む。

**標準ライブラリだけで書く。**
google-genai を入れると依存が増えてワークフローが遅くなる。REST を urllib で
叩くだけなので足りる。

**thinking は切る。**
gemini-2.5-flash は既定で内部推論にトークンを使い、その分が maxOutputTokens を
食う。JSONを返させる用途では出力が途中で切れる原因になるので 0 にする。

**429(レート上限)は待って再試行する。**
無料枠は分あたり・日あたりの上限がある。分あたりに当たっただけなら待てば通る。
日あたりの上限に当たった場合は諦めて例外を投げる。呼び出し側は失敗時に前回の
内容を引き継ぐようにしてあるので、その日はAI部分が据え置きになる。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
LIST_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200"

# 既定モデル。無料枠は Flash 系のみ。環境変数で差し替えられる。
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")

# 既定モデルが使えなかったときに順に試す候補。
FALLBACK_MODELS = ("gemini-3.8-flash", "gemini-2.5-flash")

MAX_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", "2"))
TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "90"))

# 1回の生成にかけてよい時間の上限（秒）。
# **これが無いと詰まったときに延々と待つ。**
# ただし短すぎてもいけない。無料枠は順番待ちが入るようで、実測で1回の
# 生成に13分かかったことがある（それでも中身は正しく返ってきた）。
# 更新が多少遅れても構わない運用なので、打ち切りは余裕を持たせる。
DEADLINE = int(os.getenv("GEMINI_DEADLINE", "900"))


class LLMError(RuntimeError):
    pass


class _Block:
    """Anthropic の content ブロック相当。"""

    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _Usage:
    def __init__(self, meta: dict):
        self.input_tokens = int(meta.get("promptTokenCount") or 0)
        self.output_tokens = int(meta.get("candidatesTokenCount") or 0)


class _Response:
    def __init__(self, payload: dict, model: str):
        cands = payload.get("candidates") or []
        parts = []
        if cands:
            parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        self.content = [_Block(text)]
        self.usage = _Usage(payload.get("usageMetadata") or {})
        # Anthropic の stop_reason 相当。診断ログで使っている箇所がある。
        self.stop_reason = (cands[0].get("finishReason") if cands else None)
        self.model = model


def discover_models(api_key: str) -> list[str]:
    """使えるFlash系モデルを問い合わせて返す。

    **モデル名を決め打ちにしない。**
    Google はモデルを頻繁に改廃する（gemini-2.5-flash は「新規ユーザーには
    提供されない」と言われて404になった）。固定の候補が全滅したときは、
    その場で一覧を引いて生きている名前を拾う。
    """
    req = urllib.request.Request(
        LIST_ENDPOINT, headers={"x-goog-api-key": api_key}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # 一覧すら引けないなら諦める
        print(f"  [llm] モデル一覧の取得に失敗: {e}", file=sys.stderr, flush=True)
        return []

    names = []
    for m in data.get("models", []):
        if "generateContent" not in (m.get("supportedGenerationMethods") or []):
            continue
        name = (m.get("name") or "").replace("models/", "")
        if "flash" not in name:
            continue        # 無料枠は Flash 系のみ
        if "image" in name or "tts" in name or "live" in name:
            continue        # 文章生成用ではない
        names.append(name)

    # lite は最後に回す。プレビュー版も後ろ。
    names.sort(key=lambda n: ("lite" in n, "preview" in n or "exp" in n, n))
    print(f"  [llm] 利用可能なモデル: {names[:5]}", file=sys.stderr, flush=True)
    return names


def _post(model: str, api_key: str, body: dict) -> dict:
    req = urllib.request.Request(
        ENDPOINT.format(model=model),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


class _Messages:
    def __init__(self, api_key: str):
        self._key = api_key

    def create(self, model=None, max_tokens=2000, messages=None,
               system=None, temperature=None, **_ignored):
        msgs = messages or []
        # Anthropic 形式の messages を Gemini の contents に移す。
        contents = []
        for m in msgs:
            content = m.get("content")
            if isinstance(content, list):
                # [{"type":"text","text":...}, ...] 形式
                content = "".join(b.get("text", "") for b in content
                                  if isinstance(b, dict))
            contents.append({
                "role": "model" if m.get("role") == "assistant" else "user",
                "parts": [{"text": str(content)}],
            })

        body = {
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": int(max_tokens),
                # 内部推論にトークンを使わせない（出力が途中で切れるのを防ぐ）
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        if temperature is not None:
            body["generationConfig"]["temperature"] = temperature
        if system:
            body["systemInstruction"] = {"parts": [{"text": str(system)}]}

        # model 引数は Anthropic のモデル名が渡ってくる。無視して Gemini 側を使う。
        state = {"last_err": None, "no_thinking": False}
        started = time.monotonic()

        def out_of_time():
            return time.monotonic() - started > DEADLINE

        def attempt_model(name):
            for attempt in range(MAX_RETRIES):
                if out_of_time():
                    print(f"  [llm] 制限時間({DEADLINE}秒)を超えたので打ち切ります",
                          file=sys.stderr, flush=True)
                    return None
                payload = dict(body)
                if state["no_thinking"]:
                    gc = dict(payload["generationConfig"])
                    gc.pop("thinkingConfig", None)
                    payload["generationConfig"] = gc
                try:
                    return _Response(_post(name, self._key, payload), name)
                except urllib.error.HTTPError as e:
                    detail = e.read().decode("utf-8", "replace")[:300]
                    state["last_err"] = f"{name}: HTTP {e.code} {detail}"
                    if e.code == 400 and "think" in detail.lower() \
                            and not state["no_thinking"]:
                        # thinkingConfig を受け付けないモデル。外して同じ名前で再試行。
                        print("  [llm] thinkingConfig 非対応。外して再試行します",
                              file=sys.stderr, flush=True)
                        state["no_thinking"] = True
                        continue
                    if e.code in (404, 400):
                        print(f"  [llm] {state['last_err']}", file=sys.stderr, flush=True)
                        return None
                    if e.code == 429:
                        wait = 20 * (attempt + 1)
                        print(f"  [llm] レート上限。{wait}秒待って再試行 "
                              f"({attempt + 1}/{MAX_RETRIES})", file=sys.stderr, flush=True)
                        time.sleep(wait)
                        continue
                    if 500 <= e.code < 600:
                        time.sleep(5 * (attempt + 1))
                        continue
                    print(f"  [llm] {state['last_err']}", file=sys.stderr, flush=True)
                    return None
                except (urllib.error.URLError, TimeoutError) as e:
                    state["last_err"] = f"{name}: {e}"
                    time.sleep(5 * (attempt + 1))
            return None

        tried = []
        for name in (DEFAULT_MODEL, *FALLBACK_MODELS):
            if out_of_time():
                break
            tried.append(name)
            got = attempt_model(name)
            if got is not None:
                return got

        # 決め打ちの候補が全滅。生きているモデルを問い合わせて拾い直す。
        for name in ([] if out_of_time() else discover_models(self._key)):
            if name in tried:
                continue
            got = attempt_model(name)
            if got is not None:
                return got

        raise LLMError(f"Gemini 呼び出しに失敗しました: {state['last_err']}")


class Client:
    def __init__(self, api_key: str):
        self.messages = _Messages(api_key)


def client(api_key: str | None = None) -> Client:
    """APIキーは GEMINI_API_KEY から読む。無ければ例外。"""
    key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        raise LLMError("GEMINI_API_KEY が設定されていません")
    return Client(key)
