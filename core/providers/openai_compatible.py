"""
Adapter for any OpenAI-compatible chat-completions API. DeepSeek and
Moonshot/Kimi both implement the same wire format OpenAI does (tools /
tool_calls shape included), so one adapter covers all three — point it at a
different model_id + base_url in providers/config.py and it works
unchanged. That's the bulk of "support any LLM": most non-Gemini,
non-Anthropic providers speak this dialect.
"""

import json
import time

from openai import OpenAI, RateLimitError

from .base import Provider, ProviderResponse, ToolCall, Turn

MAX_RETRIES = 5


class OpenAICompatibleProvider(Provider):
    def __init__(self, model_id: str, env_key: str, base_url: str | None = None):
        super().__init__(model_id, env_key, base_url)
        self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def generate(self, history: list[Turn], tools: list[dict], system_prompt: str) -> ProviderResponse:
        messages = [{"role": "system", "content": system_prompt}] + _to_openai_messages(history)
        oai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            }
            for t in tools
        ]
        response = self._create_with_retry(model=self.model_id, messages=messages, tools=oai_tools)

        choice = response.choices[0].message
        tool_calls = [
            ToolCall(id=tc.id, name=tc.function.name, args=json.loads(tc.function.arguments or "{}"))
            for tc in (choice.tool_calls or [])
        ]
        return ProviderResponse(text=choice.content, tool_calls=tool_calls)

    def _create_with_retry(self, **kwargs):
        for attempt in range(MAX_RETRIES):
            try:
                return self._client.chat.completions.create(**kwargs)
            except RateLimitError:
                wait = 2 ** attempt
                if attempt == MAX_RETRIES - 1:
                    raise
                print(f"\033[91mRate limited, waiting {wait}s (attempt {attempt + 1}/{MAX_RETRIES})...\033[0m")
                time.sleep(wait)


def _to_openai_messages(history: list) -> list:
    messages = []
    for turn in history:
        if turn.role == "user" and turn.tool_results:
            for tr in turn.tool_results:
                messages.append({"role": "tool", "tool_call_id": tr.call_id, "content": tr.content})
        elif turn.role == "assistant":
            msg = {"role": "assistant", "content": turn.text}
            if turn.tool_calls:
                msg["tool_calls"] = [
                    {
                        "type": "function",
                        "id": tc.id,
                        "function": {"name": tc.name, "arguments": json.dumps(tc.args)},
                    }
                    for tc in turn.tool_calls
                ]
            messages.append(msg)
        else:
            messages.append({"role": "user", "content": turn.text or ""})
    return messages
