"""
Gemini adapter. Wraps google-genai's client and translates between our
neutral Turn/ToolCall types (base.py) and Gemini's Content/Part/
FunctionDeclaration shapes.
"""

import re
import time

from google import genai
from google.genai import types

from .base import Provider, ProviderResponse, ToolCall, Turn

MAX_RETRIES = 5


class GeminiProvider(Provider):
    def __init__(self, model_id: str, env_key: str, base_url: str | None = None):
        super().__init__(model_id, env_key, base_url)
        self._client = genai.Client(api_key=self.api_key)

    def generate(self, history: list[Turn], tools: list[dict], system_prompt: str) -> ProviderResponse:
        contents = _to_gemini_contents(history)
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=[_build_gemini_tool(tools)],
            # We orchestrate the tool-call loop ourselves — turn off the SDK's
            # automatic function calling so it doesn't try to execute anything.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        response = self._generate_with_retry(contents, config)

        # Walk the raw parts, not response.function_calls, so we can carry each
        # call's thought_signature along — Gemini 3 rejects a resent call missing it.
        parts = response.candidates[0].content.parts if response.candidates else []
        fc_parts = [p for p in parts if p.function_call is not None]
        # Gemini doesn't hand back call ids — synthesize positional ones so
        # ToolResult.call_id has something stable to reference within a turn.
        tool_calls = [
            ToolCall(
                id=f"call_{i}",
                name=p.function_call.name,
                args=dict(p.function_call.args) if p.function_call.args else {},
                raw={"thought_signature": p.thought_signature} if p.thought_signature else None,
            )
            for i, p in enumerate(fc_parts)
        ]
        return ProviderResponse(text=response.text, tool_calls=tool_calls)

    def _generate_with_retry(self, contents, config):
        """Retries on 429s — the free tier's rate limit gets hit often enough
        (one request per tool round-trip) that this is expected, not a bug."""
        for attempt in range(MAX_RETRIES):
            try:
                return self._client.models.generate_content(
                    model=self.model_id, contents=contents, config=config
                )
            except Exception as e:
                msg = str(e)
                if "429" not in msg and "quota" not in msg.lower():
                    raise  # not a rate-limit error, don't swallow it

                match = re.search(r"retry in ([\d.]+)s", msg)
                wait = float(match.group(1)) + 1 if match else (2 ** attempt)

                if attempt == MAX_RETRIES - 1:
                    raise
                print(f"\033[91mRate limited, waiting {wait:.0f}s (attempt {attempt + 1}/{MAX_RETRIES})...\033[0m")
                time.sleep(wait)


def _build_gemini_tool(tool_schemas: list) -> "types.Tool":
    declarations = [
        types.FunctionDeclaration(
            name=t["name"],
            description=t["description"],
            parameters_json_schema=t["parameters"],
        )
        for t in tool_schemas
    ]
    return types.Tool(function_declarations=declarations)


def _to_gemini_contents(history: list) -> list:
    contents = []
    for turn in history:
        if turn.role == "assistant":
            parts = []
            if turn.text:
                parts.append(types.Part.from_text(text=turn.text))
            for tc in turn.tool_calls:
                part = types.Part.from_function_call(name=tc.name, args=tc.args)
                ts = (tc.raw or {}).get("thought_signature")
                if ts:
                    part.thought_signature = ts
                parts.append(part)
            contents.append(types.Content(role="model", parts=parts))
        elif turn.tool_results:
            # Gemini has no separate "tool" role — function responses go
            # back as role="user", same convention as Anthropic/OpenAI.
            parts = [
                types.Part.from_function_response(name=tr.name, response={"result": tr.content})
                for tr in turn.tool_results
            ]
            contents.append(types.Content(role="user", parts=parts))
        else:
            contents.append(types.Content(role="user", parts=[types.Part.from_text(text=turn.text or "")]))
    return contents
