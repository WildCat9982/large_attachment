"""One function between the app and the model, so the provider is easy to swap."""
import functools

import config


@functools.lru_cache(maxsize=1)
def _client():
    if config.LLM_PROVIDER == "openai":
        from openai import OpenAI
        return OpenAI(max_retries=5)  # reads OPENAI_API_KEY and OPENAI_BASE_URL
    from anthropic import Anthropic
    return Anthropic(max_retries=5)  # reads ANTHROPIC_API_KEY


def complete(system, messages, fast=False, max_tokens=2000):
    """messages: [{'role': 'user'|'assistant', 'content': str}, ...] -> reply text."""
    model = config.LLM_FAST_MODEL if fast else config.LLM_MODEL
    if config.LLM_PROVIDER == "openai":
        resp = _client().chat.completions.create(
            model=model, messages=[{"role": "system", "content": system}, *messages]
        )
        return resp.choices[0].message.content or ""
    resp = _client().messages.create(
        model=model, max_tokens=max_tokens, system=system, messages=messages
    )
    return "".join(block.text for block in resp.content if block.type == "text")
