"""RootSign — tamper-evident provenance logging for AI agents (Providex AI).

Top-level public surface — the only names users should import directly. The
internal layout (`rootsign.sdk.*`, `rootsign.ingest.*`, `rootsign.crud.*`)
remains accessible but is not part of the stable contract.
"""

from rootsign.errors import (
    EscalationDepthExceededError,
    HiTLRejectedError,
    HiTLTimeoutError,
    RootSignError,
)
from rootsign.schemas.agent import (
    AgentEnvironment,
    AgentFramework,
    AgentRiskTier,
)
from rootsign.sdk.chain import (
    VerifyResult,
    verify_session,
    verify_session_local,
)
from rootsign.sdk.buffered_client import BufferedIngestClient
from rootsign.sdk.client import (
    HttpIngestClient,
    IngestClient,
    LocalIngestClient,
    get_ingest_client,
)
from rootsign.sdk.context import SessionContext
from rootsign.sdk.decorator import trace
from rootsign.sdk.frameworks.crewai import CrewAITracer
from rootsign.sdk.frameworks.langgraph import LangGraphTracer
from rootsign.sdk.redaction import (
    FinancialPIIConfig,
    HealthcarePIIConfig,
    RedactionConfig,
    StandardPIIConfig,
)
from rootsign.sdk.registration import register_agent
from rootsign.sdk.session import session

from rootsign._version import __version__  # noqa: E402, F401  (re-export)


def wrap_tools(
    tools: list,
    *,
    ctx: SessionContext,
    client: IngestClient,
    redaction_config: RedactionConfig | None = None,
) -> list:
    """Convenience wrapper — instrument a list of LangGraph tools.

    Drop-in replacement for the input list of `ToolNode([...])`. See
    ADR-004 for the wrapping strategy.
    """
    return LangGraphTracer.wrap_tools(
        tools, ctx=ctx, client=client, redaction_config=redaction_config
    )


def wrap_crewai_tools(
    tools: list,
    *,
    ctx: SessionContext,
    client: IngestClient,
    redaction_config: RedactionConfig | None = None,
) -> list:
    """Convenience wrapper — instrument a list of CrewAI tools.

    Drop-in for `Agent(tools=[...])`. See ADR-005.
    """
    return CrewAITracer.wrap_tools(
        tools, ctx=ctx, client=client, redaction_config=redaction_config
    )


__all__ = [
    "AgentEnvironment",
    "AgentFramework",
    "AgentRiskTier",
    "BufferedIngestClient",
    "CrewAITracer",
    "EscalationDepthExceededError",
    "FinancialPIIConfig",
    "HealthcarePIIConfig",
    "HiTLRejectedError",
    "HiTLTimeoutError",
    "HttpIngestClient",
    "IngestClient",
    "LangGraphTracer",
    "LocalIngestClient",
    "RedactionConfig",
    "RootSignError",
    "SessionContext",
    "StandardPIIConfig",
    "VerifyResult",
    "get_ingest_client",
    "register_agent",
    "session",
    "trace",
    "verify_session",
    "verify_session_local",
    "wrap_crewai_tools",
    "wrap_tools",
]
