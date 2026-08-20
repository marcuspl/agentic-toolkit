"""
Gemini provider via google-genai SDK (direct Google AI API).
Uses the new google-genai package (google-generativeai is deprecated).
"""
import os
import time


def run(model: str, prompt: str, system: str = None, stream_callback=None):
    """
    Call Gemini API with streaming.

    Returns:
        (full_text, model_id, usage_dict)
        usage_dict: {input_tokens, output_tokens, thinking_tokens}
    """
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")

    client = genai.Client(api_key=api_key)

    config_kwargs = {
        "max_output_tokens": 16000,
        # gemini-2.5-* models use thinking; set a generous budget
        "thinking_config": types.ThinkingConfig(thinking_budget=8000),
    }
    if system:
        config_kwargs["system_instruction"] = system

    config = types.GenerateContentConfig(**config_kwargs)

    full_text = ""
    usage = {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0}

    for attempt in range(2):
        try:
            full_text = ""
            response = client.models.generate_content_stream(
                model=model,
                contents=prompt,
                config=config,
            )

            for chunk in response:
                text = getattr(chunk, "text", "") or ""
                if text:
                    if stream_callback:
                        stream_callback(text)
                    full_text += text

                # Usage metadata arrives on the final chunk
                um = getattr(chunk, "usage_metadata", None)
                if um and getattr(um, "candidates_token_count", None):
                    usage = {
                        "input_tokens":    getattr(um, "prompt_token_count", 0) or 0,
                        "output_tokens":   getattr(um, "candidates_token_count", 0) or 0,
                        "thinking_tokens": getattr(um, "thoughts_token_count", 0) or 0,
                    }

            return full_text, model, usage

        except Exception as e:
            err_str = str(e).lower()
            if attempt == 0 and ("quota" in err_str or "rate" in err_str or "429" in err_str):
                print("\n[runner] Rate limited (Gemini) — waiting 30s before retry...", flush=True)
                time.sleep(30)
                continue
            if attempt == 0 and ("timeout" in err_str or "deadline" in err_str):
                print("\n[runner] Timeout (Gemini) — retrying once...", flush=True)
                time.sleep(5)
                continue
            raise
