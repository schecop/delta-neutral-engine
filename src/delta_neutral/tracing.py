"""Traces for supervisor runs, sent to Langfuse as OpenTelemetry (OTLP/HTTP, JSON).

One trace per question: a root span for the run, a `generation` span per model call (model,
tokens, cost, latency) and a span per tool call (arguments and result). Spans are collected in
memory and exported once at the end of the run, in one POST:

    POST {LANGFUSE_HOST}/api/public/otel/v1/traces
    Authorization: Basic base64(public_key:secret_key)   x-langfuse-ingestion-version: 4

Why OTLP and not Langfuse's own batch API: that legacy ingestion stops accepting traces on
Langfuse Cloud on 2026-11-16 (langfuse.com/docs/compatibility), and OTLP is a standard any
other backend also reads. Why by hand and not the OpenTelemetry SDK: one JSON body over the
httpx we already use, instead of three packages, and the wire format stays visible.

Tracing never breaks an answer: an export failure is logged and dropped. Traces hold questions,
tool results and answers; never keys (rule: secrets never in traces).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

AttrValue = str | int | float | bool


@dataclass
class Span:
    name: str
    trace_id: str
    span_id: str
    parent_id: str | None
    start_ns: int
    end_ns: int = 0
    attributes: dict[str, AttrValue] = field(default_factory=dict)
    error: str | None = None

    def set(self, **attributes: Any) -> None:
        """Dotted OTel names come in with "__" for "." (gen_ai__usage__input_tokens)."""
        for key, value in attributes.items():
            self.attributes[key.replace("__", ".")] = _attr(value)


class Trace:
    def __init__(self, name: str, **attributes: Any) -> None:
        self.trace_id = secrets.token_hex(16)
        self.spans: list[Span] = []
        self.root = self._start(name, None)
        self.root.set(langfuse__trace__name=name, **attributes)

    def _start(self, name: str, parent: Span | None) -> Span:
        span = Span(
            name,
            self.trace_id,
            secrets.token_hex(8),
            parent.span_id if parent else None,
            time.time_ns(),
        )
        self.spans.append(span)
        return span

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Span]:
        span = self._start(name, self.root)
        span.set(**attributes)
        try:
            yield span
        except Exception as exc:
            span.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            span.end_ns = time.time_ns()

    def finish(self, **attributes: Any) -> None:
        self.root.set(**attributes)
        self.root.end_ns = time.time_ns()

    def to_otlp(self) -> dict[str, Any]:
        return {
            "resourceSpans": [
                {
                    "resource": {"attributes": _kv({"service.name": "delta-neutral"})},
                    "scopeSpans": [
                        {
                            "scope": {"name": "delta_neutral.supervisor"},
                            "spans": [_otlp_span(s) for s in self.spans],
                        }
                    ],
                }
            ]
        }


class LangfuseExporter:
    PATH = "/api/public/otel/v1/traces"

    def __init__(
        self, host: str, public_key: str, secret_key: str, client: httpx.AsyncClient | None = None
    ) -> None:
        auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self._headers = {"Authorization": f"Basic {auth}", "x-langfuse-ingestion-version": "4"}
        self._client = client or httpx.AsyncClient(base_url=host, timeout=httpx.Timeout(10.0))

    @classmethod
    def from_env(cls) -> LangfuseExporter | None:
        public, secret = (
            os.environ.get("LANGFUSE_PUBLIC_KEY"),
            os.environ.get("LANGFUSE_SECRET_KEY"),
        )
        if not (public and secret):
            return None
        host = os.environ.get("LANGFUSE_HOST") or "https://cloud.langfuse.com"
        return cls(host, public, secret)

    async def export(self, trace: Trace) -> bool:
        try:
            response = await self._client.post(
                self.PATH, json=trace.to_otlp(), headers=self._headers
            )
        except httpx.HTTPError as exc:
            log.warning("trace export failed: %s", type(exc).__name__)
            return False
        if response.status_code >= 300:
            log.warning(
                "trace export failed: HTTP %s %s", response.status_code, response.text[:200]
            )
            return False
        return True

    async def aclose(self) -> None:
        await self._client.aclose()


def _attr(value: Any) -> AttrValue:
    if isinstance(value, str | bool | int | float):
        return value
    return json.dumps(value, default=str)  # dicts, lists, Decimals: as JSON text


def _kv(attributes: dict[str, AttrValue]) -> list[dict[str, Any]]:
    out = []
    for key, value in attributes.items():
        if isinstance(value, bool):
            v: dict[str, Any] = {"boolValue": value}
        elif isinstance(value, int):
            v = {"intValue": str(value)}  # OTLP JSON: int64 as a string
        elif isinstance(value, float):
            v = {"doubleValue": value}
        else:
            v = {"stringValue": value}
        out.append({"key": key, "value": v})
    return out


def _otlp_span(s: Span) -> dict[str, Any]:
    span: dict[str, Any] = {
        "traceId": s.trace_id,
        "spanId": s.span_id,
        "name": s.name,
        "kind": 1,  # internal
        "startTimeUnixNano": str(s.start_ns),
        "endTimeUnixNano": str(s.end_ns or time.time_ns()),
        "attributes": _kv(s.attributes),
        "status": {"code": 2, "message": s.error} if s.error else {"code": 1},
    }
    if s.parent_id:
        span["parentSpanId"] = s.parent_id
    return span
