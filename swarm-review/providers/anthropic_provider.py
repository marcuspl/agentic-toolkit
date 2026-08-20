"""
Claude provider via Anthropic SDK (direct API).
"""
import os
import time

# Short model names used in models.conf → full Anthropic API IDs
MODEL_MAP = {
    "sonnet":  "claude-sonnet-4-6",
    "opus":    "claude-opus-4-6",
    "haiku":   "claude-haiku-4-5-20251001",
    # pass-through if already a full ID
}


def expand_model(model: str) -> str:
    return MODEL_MAP.get(model, model)


def run(model: str, prompt: str, system: str = None, stream_callback=None):
    """
    Call Claude API with streaming.

    Returns:
        (full_text, full_model_id, usage_dict)
        usage_dict: {input_tokens, output_tokens}
    """
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)
    full_model = expand_model(model)

    kwargs = {
        "model":      full_model,
        "max_tokens": 16000,
        "messages":   [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system

    full_text = ""
    usage = {"input_tokens": 0, "output_tokens": 0}

    for attempt in range(2):
        try:
            with client.messages.stream(**kwargs) as stream:
                for text in stream.text_stream:
                    if stream_callback:
                        stream_callback(text)
                    full_text += text

                final = stream.get_final_message()
                usage = {
                    "input_tokens":  final.usage.input_tokens,
                    "output_tokens": final.usage.output_tokens,
                }
            return full_text, full_model, usage

        except anthropic.RateLimitError:
            if attempt == 0:
                print("\n[runner] Rate limited — waiting 30s before retry...", flush=True)
                time.sleep(30)
                full_text = ""
                continue
            raise
        except anthropic.APITimeoutError:
            if attempt == 0:
                print("\n[runner] Timeout — retrying once...", flush=True)
                time.sleep(5)
                full_text = ""
                continue
            raise
        except anthropic.AuthenticationError as e:
            raise RuntimeError(f"Anthropic auth failed — check ANTHROPIC_API_KEY: {e}") from e
