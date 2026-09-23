from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

import httpx

from team_harness.decisions.models import DecisionRequest


class JevBackendError(RuntimeError):
    """Raised when Jev cannot return a valid typed choice."""


@dataclass(frozen=True)
class JevAnswer:
    choice: str
    confidence: float | None
    probabilities: dict[str, float]
    usage: dict[str, Any]
    latency_ms: int


class JevBackend:
    def __init__(
        self, *, api_url: str, api_key: str, model: str, timeout_s: float
    ) -> None:
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s

    async def decide(self, request: DecisionRequest) -> JevAnswer:
        payload = {
            "state": request.state,
            "questions": {
                "decision": {
                    "type": "choice",
                    "instructions": request.instructions,
                    "criteria": request.options,
                }
            },
            "model": self.model,
        }
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.post(
                    self.api_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise JevBackendError(str(exc)) from exc

        elapsed_ms = round((time.monotonic() - started) * 1000)
        answer = _extract_answer(body)
        choice = answer.get("choice")
        if not isinstance(choice, str):
            raise JevBackendError("Jev response omitted decision.choice")
        confidence = _optional_probability(answer.get("confidence"), "confidence")
        probabilities = _probabilities(answer.get("probabilities"))
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return JevAnswer(
            choice=choice,
            confidence=confidence,
            probabilities=probabilities,
            usage=usage,
            latency_ms=elapsed_ms,
        )


def _extract_answer(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise JevBackendError("Jev response must be a JSON object")
    answers = body.get("answers")
    if isinstance(answers, dict) and isinstance(answers.get("decision"), dict):
        return answers["decision"]
    results = body.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        nested = results[0].get("answers")
        if isinstance(nested, dict) and isinstance(nested.get("decision"), dict):
            return nested["decision"]
    raise JevBackendError("Jev response omitted answers.decision")


def _optional_probability(value: Any, label: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise JevBackendError(f"Jev {label} must be numeric")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise JevBackendError(f"Jev {label} must be between 0 and 1")
    return result


def _probabilities(value: Any) -> dict[str, float]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise JevBackendError("Jev probabilities must be an object")
    result: dict[str, float] = {}
    for key, probability in value.items():
        parsed = _optional_probability(probability, f"probability {key!r}")
        if parsed is not None:
            result[str(key)] = parsed
    return result
