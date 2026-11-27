"""Trusted provider adapters; importing a skill never registers a connector."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Protocol

from jsonschema.validators import validator_for

from .models import Account, Capability, ConnectorError, ConnectorResult, Credentials, ProviderManifest, RefreshResult


@dataclass(frozen=True)
class CallContext:
    account: Account
    deadline: float
    cancelled: threading.Event = field(default_factory=threading.Event)

    def remaining(self) -> float:
        """Adapters use this deadline for every HTTP request and polling step."""
        from app.agent.tool_cancellation import cancellation_requested

        if self.cancelled.is_set() or cancellation_requested():
            raise ConnectorError("cancelled")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ConnectorError("timeout")
        return remaining


class ProviderAdapter(Protocol):
    manifest: ProviderManifest

    def refresh(self, credentials: Credentials, context: CallContext) -> RefreshResult:
        """Refresh this account only; raise needs_reauth for a revoked grant."""
        ...

    def invoke(self, capability: Capability, arguments: dict, credentials: Credentials,
               context: CallContext) -> ConnectorResult:
        """Execute once, honoring the deadline; never retry a mutation blindly."""
        ...


class ProviderRegistry:
    def __init__(self):
        """Start with no enabled integrations until bundled adapters register."""
        self._providers: dict[str, ProviderAdapter] = {}
        self._lock = threading.RLock()

    def register(self, adapter: ProviderAdapter) -> None:
        """Validate schemas and refuse silent replacement of a trusted adapter."""
        manifest = adapter.manifest
        for capability in manifest.capabilities:
            if capability.parameters.get("type") != "object":
                raise ValueError("Connector tools require object parameters")
            validator_for(capability.parameters).check_schema(capability.parameters)
        with self._lock:
            if manifest.id in self._providers:
                raise ValueError("Connector provider is already registered")
            self._providers[manifest.id] = adapter

    def get(self, provider_id: str) -> ProviderAdapter:
        """Fail closed if a saved account's adapter is missing or uninstalled."""
        with self._lock:
            adapter = self._providers.get(provider_id)
        if adapter is None:
            raise ConnectorError("provider_unavailable")
        return adapter

    def manifests(self) -> list[ProviderManifest]:
        """Return copies so API consumers cannot mutate registered contracts."""
        with self._lock:
            return [p.manifest.model_copy(deep=True) for p in self._providers.values()]
