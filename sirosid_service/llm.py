"""A small OpenRouter chat-completions client (OpenAI-compatible tool calling).

Synchronous and injectable: the chat service runs it in a worker thread, and tests hand it a
scripted fake. It never logs a prompt, a completion or the API key, and error text from the
provider is not passed to users (it can echo request content).
"""
import logging
from typing import List, Optional

import httpx

log = logging.getLogger("sirosid.llm")
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class LlmError(Exception):
    """The model call failed. `user_message` is safe to show."""

    def __init__(self, user_message: str, status: int = 0):
        super().__init__(user_message)
        self.user_message, self.status = user_message, status


class OpenRouter:
    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL, client: Optional[httpx.Client] = None,
                 referer: str = "", title: str = "SIROS ID Dev", data_collection: str = "deny", timeout: float = 90.0):
        if not api_key:
            raise ValueError("an OpenRouter API key is required")
        self._key, self._base = api_key, base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)
        self._referer, self._title, self._data_collection = referer, title, data_collection

    def complete(self, model: str, messages: List[dict], tools: List[dict], max_tokens: int = 2000) -> dict:
        """Returns {"message": {...}, "usage": {"prompt_tokens", "completion_tokens"}, "model": str}."""
        body = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if tools:
            body["tools"], body["tool_choice"] = tools, "auto"
        if self._data_collection:
            # Only route to providers that do not store or train on prompts: the messages here carry the
            # user's own configs and instance details.
            body["provider"] = {"data_collection": self._data_collection}
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json", "X-Title": self._title}
        if self._referer:
            headers["HTTP-Referer"] = self._referer
        try:
            r = self._client.post(self._base + "/chat/completions", json=body, headers=headers)
        except httpx.TimeoutException:
            raise LlmError("the model took too long to answer; try again") from None
        except httpx.HTTPError:
            raise LlmError("could not reach the model provider") from None
        if r.status_code != 200:
            log.warning("openrouter status %s", r.status_code)
            raise LlmError({401: "the model provider rejected the service's credentials", 402: "the model provider's credit is exhausted",
                            429: "the model provider is rate limiting; try again shortly"}.get(r.status_code, "the model call failed"), r.status_code)
        try:
            data = r.json()
            message = data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LlmError("the model returned something unreadable") from None
        usage = data.get("usage") or {}
        return {"message": message, "model": data.get("model", model),
                "usage": {"prompt_tokens": int(usage.get("prompt_tokens") or 0), "completion_tokens": int(usage.get("completion_tokens") or 0)}}
