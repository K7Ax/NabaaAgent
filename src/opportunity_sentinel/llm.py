from __future__ import annotations

import json
import re
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Protocol

import httpx
from pydantic import BaseModel

from opportunity_sentinel.logging import logger


class StructuredLLM(Protocol):
    def generate_json(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel],
        task: str,
    ) -> BaseModel: ...


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key: str
    model: str
    headers: dict[str, str] | None = None
    body_options: dict | None = None


class ModelRouter:
    """OpenAI-compatible model router with bounded retries and provider fallback."""

    def __init__(self, providers: list[Provider], timeout: float = 20) -> None:
        if not providers:
            raise ValueError("At least one configured LLM provider is required")
        self.providers = providers
        self.client = httpx.Client(timeout=timeout)

    def generate_json(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel],
        task: str,
    ) -> BaseModel:
        errors: list[str] = []
        for provider in self.providers:
            started = time.perf_counter()
            try:
                result = self._call(provider, system, user, schema)
                logger.info(
                    "llm_call",
                    task=task,
                    provider=provider.name,
                    model=provider.model,
                    latency_ms=round((time.perf_counter() - started) * 1000, 2),
                    success=True,
                )
                return result
            except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError) as exc:
                detail = f"{provider.name}:{type(exc).__name__}:{exc}"
                errors.append(detail)
                logger.warning(
                    "llm_fallback",
                    task=task,
                    provider=provider.name,
                    latency_ms=round((time.perf_counter() - started) * 1000, 2),
                    error=str(exc),
                )
        raise RuntimeError("All LLM providers failed: " + " | ".join(errors))

    def _call(
        self,
        provider: Provider,
        system: str,
        user: str,
        schema: type[BaseModel],
    ) -> BaseModel:
        headers = {
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json",
            **(provider.headers or {}),
        }
        payload = {
            "model": provider.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": _strict_json_schema(schema.model_json_schema()),
                },
            },
        }
        payload.update(provider.body_options or {})
        response = self.client.post(
            f"{provider.base_url.rstrip('/')}/chat/completions", headers=headers, json=payload
        )
        response.raise_for_status()
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        data = content if isinstance(content, dict) else json.loads(_strip_code_fence(content))
        return schema.model_validate(data)


def build_model_router(
    *,
    groq_api_key: str | None,
    openrouter_api_key: str | None,
    groq_model: str,
    openrouter_model: str,
    timeout: float,
) -> ModelRouter | None:
    providers: list[Provider] = []
    if groq_api_key:
        providers.append(
            Provider("groq", "https://api.groq.com/openai/v1", groq_api_key, groq_model)
        )
    if openrouter_api_key:
        providers.append(
            Provider(
                "openrouter",
                "https://openrouter.ai/api/v1",
                openrouter_api_key,
                openrouter_model,
                {
                    "HTTP-Referer": "https://github.com/SDAIAAcademy",
                    "X-Title": "Opportunity Sentinel",
                },
                {
                    "provider": {"require_parameters": True},
                    "plugins": [{"id": "response-healing"}],
                },
            )
        )
    return ModelRouter(providers, timeout) if providers else None


def _strip_code_fence(value: str) -> str:
    value = value.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", value, flags=re.S | re.I)
    return match.group(1) if match else value


def _strict_json_schema(schema: dict) -> dict:
    """Convert Pydantic JSON Schema to the closed-object form strict providers require."""
    result = deepcopy(schema)

    def close_objects(node) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["additionalProperties"] = False
                node["required"] = list(properties)
            for value in node.values():
                close_objects(value)
        elif isinstance(node, list):
            for value in node:
                close_objects(value)

    close_objects(result)
    return result
