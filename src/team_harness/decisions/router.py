from __future__ import annotations

import json
import os
from typing import Protocol

from team_harness.decisions.jev_backend import JevAnswer
from team_harness.decisions.jev_backend import JevBackend
from team_harness.decisions.models import DecisionRequest
from team_harness.decisions.models import DecisionResult
from team_harness.decisions.models import DecisionRouterSettings


class DecisionBackend(Protocol):
    async def decide(self, request: DecisionRequest) -> JevAnswer: ...


class DecisionRouter:
    """Use Jev for bounded choices and preserve a caller-supplied fallback."""

    def __init__(
        self,
        settings: DecisionRouterSettings,
        *,
        backend: DecisionBackend | None = None,
    ) -> None:
        self.settings = settings
        api_key = os.environ.get("TYPESAFE_API_KEY") or os.environ.get("JEV_API_KEY")
        self._backend = backend
        self._unavailable_reason: str | None = None
        if backend is None and settings.enabled:
            if api_key:
                self._backend = JevBackend(
                    api_url=settings.api_url,
                    api_key=api_key,
                    model=settings.model,
                    timeout_s=settings.timeout_s,
                )
            else:
                self._unavailable_reason = "TYPESAFE_API_KEY or JEV_API_KEY is not set"

    async def decide(self, request: DecisionRequest) -> DecisionResult:
        input_chars = len(json.dumps(request.state, ensure_ascii=False, default=str))
        fallback = self._fallback(
            request=request,
            reason=self._unavailable_reason or "decision router is disabled",
            input_chars=input_chars,
        )
        if not self.settings.enabled or self._backend is None:
            return fallback
        try:
            answer = await self._backend.decide(request)
        except Exception as exc:
            return self._fallback(
                request=request,
                reason=f"{type(exc).__name__}: {exc}",
                input_chars=input_chars,
            )
        if answer.choice not in request.options:
            return self._fallback(
                request=request,
                reason=f"invalid choice: {answer.choice!r}",
                input_chars=input_chars,
            )
        if answer.confidence is None:
            return self._fallback(
                request=request,
                reason="Jev response omitted confidence",
                input_chars=input_chars,
            )
        if answer.confidence < self.settings.confidence_threshold:
            return self._fallback(
                request=request,
                reason=(
                    f"confidence {answer.confidence:.3f} below threshold "
                    f"{self.settings.confidence_threshold:.3f}"
                ),
                input_chars=input_chars,
                confidence=answer.confidence,
                probabilities=answer.probabilities,
                usage=answer.usage,
                latency_ms=answer.latency_ms,
            )
        return DecisionResult(
            kind=request.kind,
            choice=answer.choice,
            confidence=answer.confidence,
            applied=True,
            backend="jev",
            model=self.settings.model,
            input_chars=input_chars,
            probabilities=answer.probabilities,
            usage=answer.usage,
            latency_ms=answer.latency_ms,
        )

    def _fallback(
        self,
        *,
        request: DecisionRequest,
        reason: str,
        input_chars: int,
        confidence: float | None = None,
        probabilities: dict[str, float] | None = None,
        usage: dict | None = None,
        latency_ms: int | None = None,
    ) -> DecisionResult:
        return DecisionResult(
            kind=request.kind,
            choice=request.fallback,
            confidence=confidence,
            applied=False,
            fallback_reason=reason,
            backend="jev",
            model=self.settings.model,
            input_chars=input_chars,
            probabilities=probabilities or {},
            usage=usage or {},
            latency_ms=latency_ms,
        )
