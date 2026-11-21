"""Register and run an owned computer, without provisioning cloud resources."""

from __future__ import annotations

import asyncio
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from personal_agent.commands.common import echo, fail, opened

app = typer.Typer(help="Owned execution computers: pair, run, inspect and revoke.")
DEFAULT_NODE_HOME = "~/.personal-agent-node"


class BackendChoice(StrEnum):
    sdk = "sdk"
    cli = "cli"


@app.command("invite")
def invite(
    space: Annotated[
        list[str] | None, typer.Option("--space", help="Space ID allowed on this node.")
    ] = None,
    label: Annotated[str, typer.Option(help="A label such as laptop.")] = "",
) -> None:
    """On the control computer: issue a single-use node pairing code."""
    from personal_agent.collaboration.models import OWNER_ID, OWNER_SPACE
    from personal_agent.collaboration.store import IdentityStore
    from personal_agent.nodes.registry import NodeRegistry

    with opened() as agent:
        identities = IdentityStore(agent.db, agent.clock)
        identities.require_admin(OWNER_ID)
        spaces = space or [OWNER_SPACE]
        for space_id in spaces:
            if identities.space(space_id) is None:
                fail("Space does not exist.", code=2)
        invitation = NodeRegistry(agent.db, agent.clock).create_invite(OWNER_ID, spaces, label)
        echo("Run on your own computer: pa node pair --url " + agent.config.web.address)
        echo("Enter this one-use pairing code when prompted (expires in 10 minutes):")
        echo(invitation.code)


@app.command("pair")
def pair(
    url: Annotated[str, typer.Option(help="Control-plane HTTPS origin.")],
    node_home: Annotated[Path, typer.Option(help="Private local node state directory.")] = Path(
        DEFAULT_NODE_HOME
    ),
) -> None:
    """On the execution computer: pair using a short-lived code, saved privately."""
    from personal_agent.nodes.client import (
        Credentials,
        HttpTransport,
        TransportError,
        save_credentials,
    )
    from personal_agent.nodes.registry import DEFAULT_CAPABILITIES

    home = node_home.expanduser().resolve()
    if (home / "node.json").exists() or (home / "receipts.db").exists():
        fail(
            "This directory already belongs to a node.",
            "Recover that Runner, or revoke it and choose a new --node-home directory.",
        )
    code = typer.prompt("Pairing code", hide_input=True)

    async def redeem() -> None:
        async with HttpTransport(url) as client:
            response = await client.request(
                "POST",
                "/api/v3/nodes/pair",
                {"code": code, "capabilities": list(DEFAULT_CAPABILITIES)},
            )
        node = response.get("node")
        token = response.get("token")
        if (
            not isinstance(node, dict)
            or not isinstance(node.get("id"), str)
            or not isinstance(token, str)
            or len(token) < 32
        ):
            raise TransportError("control plane did not return valid node credentials")
        save_credentials(home, Credentials(url, node["id"], token))
        echo(f"Paired node {node['id']}. Start it with: pa node run")

    try:
        asyncio.run(redeem())
    except (TransportError, ValueError, OSError) as exc:
        fail(str(exc))


@app.command("run")
def run(
    node_home: Annotated[Path, typer.Option(help="Private local node state directory.")] = Path(
        DEFAULT_NODE_HOME
    ),
    image: Annotated[
        str,
        typer.Option(help="Docker image with /usr/bin/timeout. Pull it yourself first."),
    ] = "python:3.12-slim",
    memory_mb: Annotated[int, typer.Option(min=128, max=4096)] = 512,
    poll_s: Annotated[float, typer.Option(min=0.1, max=60)] = 2,
    backend: Annotated[
        BackendChoice,
        typer.Option(help="Docker API adapter; cli preserves the legacy implementation."),
    ] = BackendChoice.sdk,
) -> None:
    """Run the worker on this computer (needs Docker)."""
    from personal_agent.nodes.agent import Runner
    from personal_agent.nodes.backend import BackendError, DockerBackend
    from personal_agent.nodes.client import HttpTransport, TransportError, load_credentials
    from personal_agent.nodes.store import NodeStore
    from personal_agent.presence import EngineLock

    home = node_home.expanduser().resolve()
    lock = EngineLock(home / "runner.lock")
    if not lock.acquire():
        fail("A Runner is already using this node directory.")

    async def serve() -> None:
        credentials = load_credentials(home)
        with NodeStore(home) as store:
            async with HttpTransport(credentials.url, credentials.token) as client:
                selected: DockerBackend
                if backend is BackendChoice.sdk:
                    from personal_agent.nodes.docker_sdk import DockerSDKBackend

                    selected = DockerSDKBackend(image, memory_mb=memory_mb)
                else:
                    selected = DockerBackend(image, memory_mb=memory_mb)
                try:
                    await selected.probe()
                    runner = Runner(
                        store,
                        selected,
                        client,
                        known_secrets=(credentials.token,),
                    )
                    echo(
                        f"Node {credentials.node_id} is polling using {backend.value}; press Ctrl-C to stop the Runner."
                    )
                    await runner.run(poll_s=poll_s)
                finally:
                    await selected.aclose()

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        echo("Runner stopped. Retained executions remain bounded by their container deadline.")
    except (ValueError, OSError, TransportError, BackendError) as exc:
        fail(str(exc))
    finally:
        lock.release()


@app.command("status")
def status(
    node_home: Annotated[Path, typer.Option(help="Private local node state directory.")] = Path(
        DEFAULT_NODE_HOME
    ),
) -> None:
    """Show the receipts waiting on this computer."""
    from personal_agent.nodes.client import load_credentials
    from personal_agent.nodes.store import NodeStore
    from personal_agent.presence import EngineLock

    home = node_home.expanduser().resolve()
    try:
        credentials = load_credentials(home)
        echo(f"Node: {credentials.node_id}")
        echo(f"Runner: {'running' if EngineLock.is_taken(home / 'runner.lock') else 'stopped'}")
        with NodeStore(home) as store:
            pending = store.pending()
            if not pending:
                echo("No pending executions or cleanup.")
            for item in pending:
                echo(f"{item.attempt_id}  {item.state}  cleanup={item.cleanup_state}")
    except (ValueError, OSError) as exc:
        fail(str(exc))


@app.command("revoke")
def revoke(node_id: str) -> None:
    """On the control computer: revoke a node's future access."""
    from personal_agent.collaboration.models import OWNER_ID
    from personal_agent.nodes.registry import NodeRegistry

    with opened() as agent:
        if not NodeRegistry(agent.db, agent.clock).revoke(node_id, OWNER_ID):
            fail("Node not found, already revoked or owned by another principal.")
        echo(
            "Node access revoked. Offline execution stops at its local deadline; cancellation remains pending until confirmed."
        )
