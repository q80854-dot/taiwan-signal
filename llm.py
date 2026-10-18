"""多供應商 LLM 介面（2026-10-18）。

使用者不想被單一 ANTHROPIC_API_KEY 綁住，要求免費替代方案。這裡提供一個統一的
complete()，依環境變數自動挑一個「有金鑰」的供應商；全部都沒設時回 None，呼叫端
（reports.py）會自動退回規則式摘要。全部用 requests 直接打 REST，不綁任何 SDK。

支援（擇一，依出現順序，或用 LLM_PROVIDER 強制指定）：
  - gemini：Google Gemini 免費版（免費、免信用卡）。金鑰 GEMINI_API_KEY（或 GOOGLE_API_KEY）。
  - groq  ：Groq（Llama，免費、免信用卡、速度快）。金鑰 GROQ_API_KEY。
  - anthropic：Claude（付費）。金鑰 ANTHROPIC_API_KEY。

資料安全：送出的只有公開市場數據與公開新聞標題，不含機敏資料；金鑰只讀環境變數、
不寫入任何日誌或回應。
"""
from __future__ import annotations
import os
import logging

logger = logging.getLogger(__name__)

_DEFAULT_MODELS = {
    "gemini": "gemini-3.8-flash",
    "groq": "llama-3.3-70b-versatile",
    "anthropic": "claude-opus-5-5",
}
# 依序偵測；第一個有金鑰的就用。免費的排前面，符合使用者「不想被付費金鑰綁住」。
_ORDER = ["gemini", "groq", "anthropic"]


def _key(provider: str) -> str:
    if provider == "gemini":
        return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if provider == "groq":
        return (os.environ.get("GROQ_API_KEY") or "").strip()
    if provider == "anthropic":
        return (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    return ""


def provider_name() -> str | None:
    """目前要用哪個供應商（LLM_PROVIDER 可強制指定），都沒金鑰則 None。"""
    forced = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
    if forced:
        return forced if _key(forced) else None
    for p in _ORDER:
        if _key(p):
            return p
    return None


def available() -> bool:
    return provider_name() is not None


def _model(provider: str) -> str:
    env = {"gemini": "GEMINI_MODEL", "groq": "GROQ_MODEL", "anthropic": "ANTHROPIC_MODEL"}.get(provider, "")
    return (os.environ.get(env) or "").strip() or _DEFAULT_MODELS.get(provider, "")


def complete(system: str, user: str, max_tokens: int = 4000, timeout: int = 60) -> str | None:
    """用目前可用的供應商產生文字。失敗／無金鑰回 None（呼叫端退回規則式）。"""
    provider = provider_name()
    if not provider:
        return None
    try:
        if provider == "gemini":
            return _gemini(system, user, max_tokens, timeout)
        if provider == "groq":
            return _groq(system, user, max_tokens, timeout)
        if provider == "anthropic":
            return _anthropic(system, user, max_tokens, timeout)
    except Exception as e:
        logger.warning(f"llm.complete({provider}) 失敗，退回規則式：{e}")
        return None
    return None


# ── Google Gemini（免費）──────────────────────────────
import re as _re


def _gemini_once(model: str, system: str, user: str, max_tokens: int, timeout: int):
    """回傳 (text, retry_model, transient)。
      - text 有值＝成功。
      - retry_model 有值＝模型已淘汰、Google 在錯誤訊息裡建議改用的新模型名。
      - transient=True＝暫時性錯誤（429 配額/509 過載/5xx），可退避後重試。"""
    import requests
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.4},
    }
    r = requests.post(url, params={"key": _key("gemini")}, json=body, timeout=timeout)
    if r.status_code != 200:
        logger.warning(f"gemini HTTP {r.status_code}: {r.text[:200]}")
        # 模型被淘汰時 Google 會在訊息裡建議新模型（例如「use models/gemini-3.8-flash」）。
        m = _re.search(r"use models/([A-Za-z0-9.\-]+)", r.text or "")
        if r.status_code == 404 and m and m.group(1) != model:
            return None, m.group(1), False
        # 429=配額、503=模型過載、500/502/504=暫時性服務錯誤 → 退避後重試
        transient = r.status_code in (429, 500, 502, 503, 504)
        return None, None, transient
    data = r.json()
    cands = data.get("candidates") or []
    if not cands:
        logger.warning(f"gemini 無 candidates（可能被安全過濾）：{str(data)[:200]}")
        return None, None, False
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    return (text or None), None, False


def _gemini(system: str, user: str, max_tokens: int, timeout: int) -> str | None:
    import time
    model = _model("gemini")
    for attempt in range(4):
        text, retry_model, transient = _gemini_once(model, system, user, max_tokens, timeout)
        if text is not None:
            return text
        if retry_model and retry_model != model:
            logger.info(f"gemini 模型 {model} 已淘汰，改用 Google 建議的 {retry_model} 重試")
            model = retry_model
            continue
        if transient and attempt < 3:
            delay = 2 * (attempt + 1)   # 2s, 4s, 6s 退避
            logger.info(f"gemini 暫時性錯誤，{delay}s 後重試（第 {attempt + 1} 次）")
            time.sleep(delay)
            continue
        return None
    return None


# ── Groq（免費，OpenAI 相容）──────────────────────────
def _groq(system: str, user: str, max_tokens: int, timeout: int) -> str | None:
    import requests
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {_key('groq')}", "Content-Type": "application/json"},
        json={"model": _model("groq"),
              "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
              "max_tokens": max_tokens, "temperature": 0.4},
        timeout=timeout,
    )
    if r.status_code != 200:
        logger.warning(f"groq HTTP {r.status_code}: {r.text[:200]}")
        return None
    data = r.json()
    ch = data.get("choices") or []
    if not ch:
        return None
    return (ch[0].get("message") or {}).get("content", "").strip() or None


# ── Anthropic Claude（付費，用 SDK）────────────────────
def _anthropic(system: str, user: str, max_tokens: int, timeout: int) -> str | None:
    try:
        import anthropic
    except Exception:
        return None
    client = anthropic.Anthropic()
    effort = (os.environ.get("REPORT_AI_EFFORT") or "medium").strip() or "medium"
    kwargs = dict(model=_model("anthropic"), max_tokens=max_tokens, system=system,
                  messages=[{"role": "user", "content": user}])
    try:
        resp = client.messages.create(output_config={"effort": effort}, **kwargs)
    except Exception:
        resp = client.messages.create(**kwargs)
    if getattr(resp, "stop_reason", None) == "refusal":
        return None
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip() or None
