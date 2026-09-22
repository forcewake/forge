"""REAL interactive-driver clients for the execution lane.

Each module implements one of the Protocol duck types declared in
:mod:`forge.adaptive.adapters` against the vendor's actual surface —
the Claude Agent SDK (Python), the Codex App Server (JSON-RPC over
stdio), and the OpenCode server (HTTP + SSE). The adapter contracts in
``adapters.py`` stay frozen; these are the real clients injected into
them. Research basis: ``docs/research/{claude-sdk-python, codex-app-server,
opencode-server, forge-harness-hacks}.md``.

Import cost policy: the claude module guards its vendor import (the
package lives in the ``interactive`` extra) — importing THIS package
never requires any vendor dependency. The codex and copilot modules are
pure asyncio stdlib; the opencode module rides the httpx dependency forge
already has.
"""

from forge.adaptive.drivers.claude_sdk import (
    ClaudeSDKDriverClient,
    claude_sdk_client_from_env,
)
from forge.adaptive.drivers.codex_app import (
    CodexAppDriverClient,
    CodexAppError,
    CodexAppTimeoutError,
    CodexConnectionClosedError,
    NoActiveTurnError,
    StdioTransport,
    TurnInProgressError,
    codex_app_client_from_env,
)
from forge.adaptive.drivers.copilot_acp import (
    CopilotACPDriverClient,
    CopilotACPError,
    CopilotACPTimeoutError,
    CopilotConnectionClosedError,
    TurnInProgressError as CopilotTurnInProgressError,
    copilot_acp_client_from_env,
)
from forge.adaptive.drivers.opencode import (
    KNOWN_EVENT_TYPES,
    OpenCodeDriverClient,
    SpecProbe,
    opencode_client_from_env,
)
from forge.adaptive.drivers.opencode_serve import (
    OpenCodeServer,
    OpenCodeServerError,
    opencode_server_from_env,
)
from forge.adaptive.drivers.live_registrations import (
    LIVE_OBSERVED_CAPABILITIES,
    LIVE_REGISTRATIONS,
    LiveRegistration,
    ObservedCapabilities,
    observed_capabilities,
    seed_live_matrix,
)

__all__ = [
    # claude-sdk (vendor package optional — the ``interactive`` extra)
    "ClaudeSDKDriverClient",
    "claude_sdk_client_from_env",
    # codex-app (pure asyncio stdlib, JSON-RPC 2.0 over stdio JSONL)
    "CodexAppDriverClient",
    "CodexAppError",
    "CodexAppTimeoutError",
    "CodexConnectionClosedError",
    "NoActiveTurnError",
    "StdioTransport",
    "TurnInProgressError",
    "codex_app_client_from_env",
    # copilot-acp (pure asyncio stdlib, JSON-RPC 2.0 ACP over stdio NDJSON;
    # the codex TurnInProgressError stays the package-level name — the
    # copilot twin rides under an explicit alias, same refusal semantics)
    "CopilotACPDriverClient",
    "CopilotACPError",
    "CopilotACPTimeoutError",
    "CopilotConnectionClosedError",
    "CopilotTurnInProgressError",
    "copilot_acp_client_from_env",
    # opencode-server (httpx against the ``opencode serve`` HTTP API)
    "KNOWN_EVENT_TYPES",
    "OpenCodeDriverClient",
    "SpecProbe",
    "opencode_client_from_env",
    # the lane-local ``opencode serve`` process lifecycle
    "OpenCodeServer",
    "OpenCodeServerError",
    "opencode_server_from_env",
    # the live-verified DriverMatrix seed (EXE-07's honest half) + the
    # versioned observed-capability rows (NXT-27)
    "LIVE_OBSERVED_CAPABILITIES",
    "LIVE_REGISTRATIONS",
    "LiveRegistration",
    "ObservedCapabilities",
    "observed_capabilities",
    "seed_live_matrix",
]
