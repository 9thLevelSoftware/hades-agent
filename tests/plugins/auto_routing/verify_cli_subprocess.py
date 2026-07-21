"""Real subprocess harness for the auto-routing verification CLI contract."""

from __future__ import annotations

import argparse
import os

from plugins.auto_routing.auto_routing.adapters.base import (
    AccessVerification,
    AdapterInventory,
    ProviderInventoryRow,
    ResolvedRuntime,
)
from plugins.auto_routing.auto_routing.cli import auto_routing_command, build_parser
from plugins.auto_routing.auto_routing.models import AccessEconomics
from plugins.auto_routing.auto_routing.service import AutoRoutingService


class SubprocessAdapter:
    """Complete deterministic adapter whose credential remains process-local."""

    observed_at = "2026-07-16T12:00:00Z"

    def inventory(self, refresh: bool = False) -> AdapterInventory:
        del refresh
        economics = AccessEconomics(
            billing_kind="metered",
            metered_input_usd_per_million_tokens=1.0,
            metered_output_usd_per_million_tokens=2.0,
            source_id="subprocess-metered-prices",
            evidence_ttl_seconds=31_536_000,
            provenance="subprocess-test-provider",
            confidence=1.0,
            observed_at=self.observed_at,
        )
        return AdapterInventory(
            provider_rows=(
                ProviderInventoryRow(
                    provider="subprocess-provider",
                    resolver_name="subprocess-provider",
                    models=("subprocess-model",),
                    authenticated=True,
                    live_attempt_status="not_attempted",
                    model_provenance={"subprocess-model": None},
                    provenance_details={"subprocess-model": {}},
                    auth_identity="api-key:subprocess",
                    credential_pool_identity="pool:subprocess",
                    endpoint_identity="endpoint:subprocess",
                    credential_fingerprint="credential:subprocess",
                    api_mode="chat_completions",
                    capabilities={
                        "subprocess-model": {"supports_tools": True}
                    },
                    economics={"subprocess-model": economics},
                    observed_at=self.observed_at,
                ),
            ),
            local_rows=(),
        )

    def resolve(self, runtime_key):
        return ResolvedRuntime(
            runtime_key=runtime_key,
            resolver_name="subprocess-provider",
            provider="subprocess-provider",
            api_mode="chat_completions",
            source="subprocess-test",
            base_url="https://example.com/v1",
            api_key=os.environ["AUTO_ROUTING_SUBPROCESS_SECRET"],
        )

    def verify_access(self, resolved_runtime, request):
        return AccessVerification(
            runtime_key=resolved_runtime.runtime_key,
            sentinel="AUTO_ROUTING_ACCESS_OK",
            response_model=resolved_runtime.runtime_key.model,
            input_tokens=min(4, request.maximum_input_tokens),
            output_tokens=1,
            actual_cost_usd=0.00001,
            response_hash="response:subprocess",
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    build_parser(parser)
    args = parser.parse_args()
    service = AutoRoutingService.from_plugin_context(
        None,
        adapter=SubprocessAdapter(),
    )
    try:
        return auto_routing_command(args, service=service)
    finally:
        service.store.close()


if __name__ == "__main__":
    raise SystemExit(main())
