"""Packaged access to the same standard-library bridge shipped to remote agents."""

from personal_agent.agent_adapters.bridge import (
    BridgeError,
    Credentials,
    RequestError,
    WorkerBridge,
    WorkerClient,
    control_origin,
    load_credentials,
    public_result,
    setup_credentials,
    update_credentials,
)

__all__ = [
    "BridgeError",
    "Credentials",
    "RequestError",
    "WorkerBridge",
    "WorkerClient",
    "control_origin",
    "load_credentials",
    "public_result",
    "setup_credentials",
    "update_credentials",
]
