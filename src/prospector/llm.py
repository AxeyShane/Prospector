"""Model access, routed per agent.

Two engines, both speaking the OpenAI chat-completions protocol, so they are
one client with a different base URL:

    local   a CPU model running on this PC, free, set up by Prospector itself
    cloud   OpenRouter, for the judgement calls worth paying for

Which agent goes where is decided in `agents.py`, not here. This module's job
is to hand back a working client for a named agent and to fail with a sentence
the user can act on.

The retry and JSON-recovery logic is deliberately forgiving. A small local
model wraps JSON in prose more often than a large one does, and a dropped call
is a company that silently never gets researched.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

import threading

import httpx

from prospector import agents

log = logging.getLogger(__name__)

# Three, not five. Retries multiply with the per-stage attempt counter: five
# retries against three attempts is fifteen billed calls for one company at one
# stage, and a model that reliably returns malformed JSON burned that budget on
# every row without anything on screen saying so.
_MAX_RETRIES = 3
_CLOUD_TIMEOUT = 120
_LOCAL_TIMEOUT = 900      # a small model on a slow CPU is not quick
_BASE_WAIT = 8

# A provider is allowed to ask us to wait, but not to park the app for an hour.
# Retry-After was passed to sleep() unclamped; a provider answering "3600" on
# four retries is four hours in a call the user cannot see or cancel.
_MAX_RETRY_AFTER = 90

# Rough per-million-token prices for the models offered in the picker, used only
# for the running estimate shown during a run. Deliberately generous: a number
# that undershoots is worse than no number.
_PRICE_PER_MTOK = {
    "google/gemini-2.5-flash": (0.30, 2.50),
    "anthropic/claude-3.5-haiku": (0.80, 4.00),
    "openai/gpt-4o-mini": (0.15, 0.60),
    "meta-llama/llama-3.3-70b-instruct": (0.12, 0.30),
}
_DEFAULT_PRICE = (0.50, 2.00)

_spend_lock = threading.Lock()
_spend = {"prompt_tokens": 0, "completion_tokens": 0, "usd": 0.0, "calls": 0}


def record_usage(model: str, usage: dict) -> None:
    """Accumulate what a cloud call cost. Never raises."""
    try:
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        return
    price_in, price_out = _PRICE_PER_MTOK.get(model, _DEFAULT_PRICE)
    cost = (prompt * price_in + completion * price_out) / 1_000_000
    with _spend_lock:
        _spend["prompt_tokens"] += prompt
        _spend["completion_tokens"] += completion
        _spend["usd"] += cost
        _spend["calls"] += 1


def spend_so_far() -> dict:
    with _spend_lock:
        return dict(_spend)


def reset_spend() -> None:
    with _spend_lock:
        _spend.update({"prompt_tokens": 0, "completion_tokens": 0,
                       "usd": 0.0, "calls": 0})


class BudgetExceeded(RuntimeError):
    """The run reached the spending limit set on the plan."""


def check_budget(limit_usd: float) -> None:
    """Raise once the run has spent its limit. No limit means no ceiling."""
    if limit_usd and spend_so_far()["usd"] >= limit_usd:
        raise BudgetExceeded(
            f"This run has reached its spending limit of ${limit_usd:.2f}. "
            f"Everything found so far is saved. Raise the limit on the plan, "
            f"or move some work to this PC, and press Continue."
        )


class LLMError(RuntimeError):
    """The model could not be reached, or returned nothing usable."""


_gate_lock = threading.Lock()
_gate: threading.Semaphore | None = None


def _local_gate() -> threading.Semaphore:
    """One semaphore per process, sized to the local server's slot count."""
    global _gate
    with _gate_lock:
        if _gate is None:
            from prospector.engine import LOCAL_PARALLEL
            _gate = threading.Semaphore(LOCAL_PARALLEL)
        return _gate


def local_ready() -> bool:
    return bool(os.environ.get("LOCAL_BASE_URL") and os.environ.get("LOCAL_MODEL"))


def cloud_ready() -> bool:
    return bool(os.environ.get("OPENROUTER_API_KEY"))


def describe() -> str:
    """One line for the app's status bar.

    No model id. This string is on screen on every page, and it used to read
    "cloud (google/gemini-2.5-flash)" -- a piece of information the intended
    user cannot use and did not ask for.
    """
    local, cloud = local_ready(), cloud_ready()
    if local and cloud:
        return "AI ready - this PC and the cloud"
    if local:
        return "AI ready on this PC"
    if cloud:
        return "Cloud AI ready"
    return "AI not set up yet"


def _endpoint(where: str) -> tuple[str, str, str, int]:
    """(base_url, model, api_key, timeout) for "local" or "cloud"."""
    if where == "local":
        base = (os.environ.get("LOCAL_BASE_URL") or "").rstrip("/")
        model = os.environ.get("LOCAL_MODEL") or ""
        if not base or not model:
            raise LLMError(
                "Local AI is not set up. Open the AI screen and press "
                "Set up local AI, or add a cloud key instead."
            )
        return base, model, "", _LOCAL_TIMEOUT

    base = (os.environ.get("OPENROUTER_BASE_URL")
            or "https://openrouter.ai/api/v1").rstrip("/")
    model = os.environ.get("OPENROUTER_MODEL") or "google/gemini-2.5-flash"
    key = os.environ.get("OPENROUTER_API_KEY") or ""
    if not key:
        raise LLMError(
            "No cloud key yet. Open the AI screen and paste your OpenRouter "
            "key - you can create one free at openrouter.ai/keys."
        )
    return base, model, key, _CLOUD_TIMEOUT


class LLMClient:
    """OpenAI-compatible chat client with retries and JSON recovery."""

    def __init__(self, base_url: str, model: str, api_key: str, timeout: int,
                 where: str = "cloud") -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.where = where
        self.is_local = where == "local"
        self._client = httpx.Client(timeout=timeout)

    def _post(self, messages: list[dict], temperature: float, max_tokens: int,
              json_mode: bool) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers.setdefault("HTTP-Referer", "https://prospector.local")
            headers.setdefault("X-Title", "Prospector")

        payload: dict = {"model": self.model, "messages": messages,
                         "temperature": temperature, "max_tokens": max_tokens}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self.is_local:
            # Hybrid-thinking models (Qwen3+, SmolLM3) otherwise spend the whole
            # token budget on hidden reasoning and return empty content.
            payload["chat_template_kwargs"] = {"enable_thinking": False}

        # The local server has a fixed number of slots. Sending more requests
        # than that does not make it faster -- they queue inside the server,
        # where the client timeout is still counting against them, so a busy
        # stage produced timeouts that looked like a model too slow for the
        # machine. Holding them here keeps the wait visible and bounded.
        gate = _local_gate() if self.is_local else None
        if gate:
            gate.acquire()
        try:
            resp = self._client.post(f"{self.base_url}/chat/completions",
                                     json=payload, headers=headers)
        finally:
            if gate:
                gate.release()
        resp.raise_for_status()
        data = resp.json()

        if not self.is_local and isinstance(data.get("usage"), dict):
            record_usage(self.model, data["usage"])

        if "choices" not in data:
            # OpenRouter reports upstream provider failures in an error object
            # with a 200 status, which would otherwise read as a KeyError.
            raise LLMError(f"The AI returned no answer: "
                           f"{str(data.get('error') or data)[:250]}")
        return data["choices"][0]["message"].get("content") or ""

    def chat(self, messages: list[dict], temperature: float = 0.0,
             max_tokens: int = 2048, json_mode: bool = False) -> str:
        last: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            try:
                return self._post(messages, temperature, max_tokens, json_mode)

            except httpx.HTTPStatusError as exc:
                resp, last = exc.response, exc
                if resp.status_code in (429, 500, 502, 503, 529) and attempt < _MAX_RETRIES - 1:
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        wait = float(retry_after) if retry_after else 0.0
                    except (TypeError, ValueError):
                        wait = 0.0
                    wait = min(wait, _MAX_RETRY_AFTER) or min(_BASE_WAIT * (2 ** attempt), 60)
                    log.warning("AI HTTP %s - waiting %.0fs (retry %d/%d)",
                                resp.status_code, wait, attempt + 1, _MAX_RETRIES)
                    time.sleep(wait)
                    continue
                if resp.status_code in (401, 403):
                    raise LLMError(
                        "The cloud service rejected your key. Open the AI screen "
                        "and paste it again from openrouter.ai/keys."
                    ) from exc
                if resp.status_code == 402:
                    raise LLMError(
                        "Your cloud account is out of credit. Add credit, switch "
                        "to a cheaper model, or move this work to this PC on the "
                        "AI screen."
                    ) from exc
                if resp.status_code == 404:
                    raise LLMError(
                        f"The model '{self.model}' is not available. Pick a "
                        f"different one on the AI screen."
                    ) from exc
                raise LLMError(f"AI error {resp.status_code}: {resp.text[:200]}") from exc

            except httpx.TimeoutException as exc:
                last = exc
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(min(_BASE_WAIT * (2 ** attempt), 60))
                    continue
                raise LLMError(
                    "The local AI timed out. This PC may be too slow for the "
                    "chosen model - try a smaller one, or move this work to the "
                    "cloud on the AI screen."
                    if self.is_local else
                    "The cloud AI timed out. Try again in a moment."
                ) from exc

            except httpx.ConnectError as exc:
                raise LLMError(
                    "The local AI has stopped. Open the AI screen and press "
                    "Set up again."
                    if self.is_local else
                    "Could not reach the cloud AI. Check your internet connection."
                ) from exc

        raise LLMError(f"The AI failed after {_MAX_RETRIES} attempts: {last}")

    def ask_json(self, prompt: str, *, system: str = "", max_tokens: int = 2048,
                 temperature: float = 0.0) -> dict:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return extract_json(self.chat(messages, temperature=temperature,
                                      max_tokens=max_tokens, json_mode=True))

    def close(self) -> None:
        self._client.close()


def extract_json(raw: str) -> dict:
    """Pull a JSON object out of a model response.

    Small models wrap JSON in prose or code fences often enough that parsing
    the raw string alone loses a noticeable share of calls.
    """
    if not raw or not raw.strip():
        raise LLMError("The AI returned an empty answer.")

    text = raw.strip()

    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {"items": data}
    except json.JSONDecodeError:
        pass

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    raise LLMError(f"Could not read the AI's answer as data: {raw[:200]}")


_clients: dict[str, LLMClient] = {}
# Every stage runs its agents on a thread pool, so this cache is read and
# written concurrently and cleared from the app's request handlers.
_clients_lock = threading.Lock()


class AgentClient:
    """A client bound to one agent, carrying that agent's role and budget.

    Stages call `ask_json(prompt)` and never think about which engine answered,
    which is what lets a single stage run locally in one project and in the
    cloud in the next without touching its code.
    """

    def __init__(self, agent_name: str) -> None:
        self.agent = agents.AGENTS.get(agent_name) or agents.AGENTS["classify"]
        self.where = agents.route(agent_name)
        base, model, key, timeout = _endpoint(self.where)

        cache_key = f"{self.where}|{base}|{model}"
        with _clients_lock:
            client = _clients.get(cache_key)
            if client is None:
                log.info("agent %s -> %s (%s)", agent_name, self.where, model)
                client = LLMClient(base, model, key, timeout, where=self.where)
                _clients[cache_key] = client
        self.client = client

    @property
    def model(self) -> str:
        return self.client.model

    def ask_json(self, prompt: str, *, system: str = "", max_tokens: int = 0,
                 temperature: float | None = None) -> dict:
        return self.client.ask_json(
            prompt,
            system=system or self.agent.role,
            max_tokens=max_tokens or self.agent.max_tokens,
            temperature=self.agent.temperature if temperature is None else temperature,
        )

    def chat(self, messages: list[dict], **kwargs) -> str:
        return self.client.chat(messages, **kwargs)


def get_client(agent_name: str = "classify") -> AgentClient:
    """The client for a named agent, routed per `agents.route`."""
    return AgentClient(agent_name)


def reset_clients() -> None:
    """Drop cached clients so a settings change takes effect immediately.

    The old clients are *dropped*, not closed. Worker threads hold references to
    them, and closing an httpx client out from under an in-flight request raises
    "Cannot send a request, as the client has been closed" -- which `run_batch`
    records as a per-company failure and counts towards the retry limit. Pressing
    "Test connection" or changing the model during a run could therefore burn
    three attempts on a company and drop it for good.

    Letting them be garbage collected closes their sockets soon enough, and
    never mid-request.
    """
    with _clients_lock:
        _clients.clear()
