"""
OpenAI-compatible providers:
  - GPT via OpenRouter  (set OPENROUTER_API_KEY, use "openai/gpt-*" model IDs)
  - Grok via xAI direct (set GROK_API_KEY,       use "grok-*" model IDs)
"""
import os
import time

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
XAI_BASE        = "https://api.x.ai/v1"


def _normalize_openrouter_model(model: str) -> str:
    """Ensure OpenRouter model IDs have the provider/ prefix."""
    if "/" in model:
        return model
    # Common bare names → OpenRouter namespaced IDs
    PREFIX_MAP = {
        "gpt-4o":         "openai/gpt-4o",
        "gpt-4o-mini":    "openai/gpt-4o-mini",
        "gpt-4.1":        "openai/gpt-4.1",
        "gpt-4.1-mini":   "openai/gpt-4.1-mini",
        "gpt-5":          "openai/gpt-5",
        "gpt-5.3":        "openai/gpt-5.3",
        "gpt-5.3-instant":"openai/gpt-5.3-instant",
        "gpt-5.4-medium": "openai/gpt-5.4-medium",
        "gpt-5.4-high":   "openai/gpt-5.4-high",
    }
    return PREFIX_MAP.get(model, f"openai/{model}")


def _run(client, model: str, prompt: str, system: str = None, stream_callback=None):
    """Shared streaming call for any OpenAI-compat client."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    full_text = ""
    usage = {"input_tokens": 0, "output_tokens": 0}

    stream = client.chat.completions.create(
        model=model,
        messages=messages,
        stream=True,
        stream_options={"include_usage": True},
        max_tokens=16000,
    )
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
            text = chunk.choices[0].delta.content
            if stream_callback:
                stream_callback(text)
            full_text += text
        if hasattr(chunk, "usage") and chunk.usage:
            usage = {
                "input_tokens":  getattr(chunk.usage, "prompt_tokens", 0) or 0,
                "output_tokens": getattr(chunk.usage, "completion_tokens", 0) or 0,
            }

    return full_text, model, usage


def run_openrouter(model: str, prompt: str, system: str = None, stream_callback=None):
    """
    GPT via OpenRouter.
    Requires OPENROUTER_API_KEY env var.
    """
    from openai import OpenAI, RateLimitError, APITimeoutError, AuthenticationError

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set")

    client = OpenAI(
        api_key=api_key,
        base_url=OPENROUTER_BASE,
        default_headers={
            "HTTP-Referer": "https://github.com/anthropics/claude-code",
            "X-Title":      "research-pipeline",
        },
    )
    norm_model = _normalize_openrouter_model(model)

    # Close the client deterministically. The sync OpenAI client wraps an httpx
    # connection pool; if it's left to garbage-collection its __del__ can fire
    # during thread/interpreter teardown after asyncio module globals are cleared,
    # surfacing a confusing "name 'base_events' is not defined" that MASKS the real
    # error (timeout / rate-limit) when many calls run concurrently in daemon threads.
    try:
        for attempt in range(2):
            try:
                return _run(client, norm_model, prompt, system, stream_callback)
            except RateLimitError:
                if attempt == 0:
                    print("\n[runner] Rate limited (OpenRouter) — waiting 30s...", flush=True)
                    time.sleep(30)
                    continue
                raise
            except APITimeoutError:
                if attempt == 0:
                    print("\n[runner] Timeout (OpenRouter) — retrying...", flush=True)
                    time.sleep(5)
                    continue
                raise
            except AuthenticationError as e:
                raise RuntimeError(f"OpenRouter auth failed — check OPENROUTER_API_KEY: {e}") from e
    finally:
        client.close()


def run_xai(model: str, prompt: str, system: str = None, stream_callback=None):
    """
    Grok via xAI direct API.
    Requires GROK_API_KEY env var.
    """
    from openai import OpenAI, RateLimitError, APITimeoutError, AuthenticationError

    api_key = os.environ.get("GROK_API_KEY")
    if not api_key:
        raise RuntimeError("GROK_API_KEY not set")

    client = OpenAI(
        api_key=api_key,
        base_url=XAI_BASE,
    )

    # See run_openrouter: close the client deterministically to avoid a
    # teardown-time "name 'base_events' is not defined" masking the real error.
    try:
        for attempt in range(2):
            try:
                return _run(client, model, prompt, system, stream_callback)
            except RateLimitError:
                if attempt == 0:
                    print("\n[runner] Rate limited (xAI) — waiting 30s...", flush=True)
                    time.sleep(30)
                    continue
                raise
            except APITimeoutError:
                if attempt == 0:
                    print("\n[runner] Timeout (xAI) — retrying...", flush=True)
                    time.sleep(5)
                    continue
                raise
            except AuthenticationError as e:
                raise RuntimeError(f"xAI auth failed — check GROK_API_KEY: {e}") from e
    finally:
        client.close()
