"""Mutations for closing the connections to the model and speech services when the services stop.

Run: uv run python scripts/mutation/run.py scripts/mutation/lists/iteration7_clients.py
Format and meaning: scripts/mutation/run.py.
"""

# ruff: noqa

AP = "src/personal_agent/app.py"
MO = "src/personal_agent/harness/models.py"
TR = "src/personal_agent/channels/transcribe.py"
SU = "src/personal_agent/commands/setup.py"

CT = ["tests/test_model_clients.py"]
DL = ["tests/test_doctor_live.py"]

M = [
    (
        "stop: the clients are left open",
        AP,
        "            if isinstance(held, ClosesConnections):",
        "            if False:",
        CT,
    ),
    (
        "stop: only the first holder is closed",
        AP,
        "        for held in holders:",
        "        for held in holders[:1]:",
        CT,
    ),
    (
        "stop: one that fails stops the rest",
        AP,
        '                except Exception:\n                    log.warning("could not close',
        '                except ZeroDivisionError:\n                    log.warning("could not close',
        CT,
    ),
    (
        "stop: the model factory is not asked",
        AP,
        "(self.model_factory, self.transcriber)",
        "(self.transcriber,)",
        CT,
    ),
    (
        "stop: the speech client is not asked",
        AP,
        "(self.model_factory, self.transcriber)",
        "(self.model_factory,)",
        CT,
    ),
    (
        "models: the client is not closed",
        MO,
        "        if client is not None:\n            await client.close()",
        "        if client is not None:\n            pass",
        CT,
    ),
    (
        "models: the closed client is kept",
        MO,
        "        client, self._client = self._client, None\n",
        "        client = self._client\n",
        CT,
    ),
    (
        "speech: the client is not closed",
        TR,
        "        if client is not None:\n            await client.close()",
        "        if client is not None:\n            pass",
        CT,
    ),
    (
        "speech: the closed client is kept",
        TR,
        "        client, self._client = self._client, None\n",
        "        client = self._client\n",
        CT,
    ),
    (
        "doctor --live: the services are left running",
        SU,
        "        await agent.stop_services()",
        "        pass",
        DL,
    ),
    (
        "doctor --live: they are stopped only when the check went well",
        SU,
        "    try:\n        return await live_checks(agent)\n    finally:\n        await agent.stop_services()",
        "    result = await live_checks(agent)\n    await agent.stop_services()\n    return result",
        DL,
    ),
]
