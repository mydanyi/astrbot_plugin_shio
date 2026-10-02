"""Tool-free auxiliary calls through existing AstrBot Provider instances.

The caller owns the deadline, cancellation and event binding. Stream fragments
are never sent or parsed: AstrBot Providers emit the complete LLMResponse with
is_chunk=False after their deltas. Appending that response to deltas duplicates
the reply, and accepting deltas without a final response accepts broken streams.
"""
from contextlib import aclosing
from typing import Any


async def auxiliary_response(
    provider: Any, *, prompt: str, settings: Any, streaming: bool | None = None
) -> Any:
    selected = settings.get("stream_provider_ids", []) if isinstance(settings, dict) else []
    config = getattr(provider, "provider_config", {})
    provider_id = config.get("id") if isinstance(config, dict) else None
    use_stream = (
        isinstance(selected, list)
        and isinstance(provider_id, str)
        and bool(provider_id)
        and provider_id in selected
    )
    # An explicit module switch (including False) wins over the saved 0.5.1.9
    # provider list. Missing switches preserve in-flight/previous settings.
    if isinstance(streaming, bool):
        use_stream = streaming
    if not use_stream:
        return await provider.text_chat(prompt=prompt, func_tool=None)

    final = None
    async with aclosing(provider.text_chat_stream(prompt=prompt, func_tool=None)) as stream:
        async for response in stream:
            if getattr(response, "role", None) == "err":
                raise ValueError("Auxiliary streaming provider returned an error")
            if getattr(response, "is_chunk", True) is False:
                final = response
    if final is None:
        raise ValueError("Auxiliary stream ended without a final response")
    return final
