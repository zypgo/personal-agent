"""Files created on first run. Existing files are never overwritten."""

from __future__ import annotations

from personal_agent.memory.store import (
    INDEX_PATH,
    INSTRUCTIONS_PATH,
    PROFILE_PATH,
    PROPOSALS_PATH,
    Actor,
    MemoryStore,
)

TEMPLATES: dict[str, str] = {
    INSTRUCTIONS_PATH: (
        "# Standing instructions\n"
        "<!-- Only you can edit this file. The agent re-reads it at the start of every run and cannot\n"
        "     change it. Write rules as plain sentences, one per line, for example:\n"
        "       Reply in Chinese unless I write in another language.\n"
        "       Ask before contacting anyone on my behalf.\n"
        "       Do not message me between 23:00 and 08:00 unless it is urgent. -->\n"
    ),
    PROFILE_PATH: (
        "# About me\n"
        "<!-- Facts the agent should always know: name, languages, location, work, people, routines.\n"
        "     Only you can edit this file. -->\n"
    ),
    INDEX_PATH: (
        "# Memory index\n\n"
        "This file is loaded into every run, so keep it short: one line per topic, each pointing to a\n"
        "detail file under /memories/state/.\n"
    ),
    PROPOSALS_PATH: (
        "# Proposals for the owner\n\n"
        "The agent appends suggested changes to your standing instructions here. Review them and edit\n"
        "/memories/preferences/ yourself if you agree.\n"
    ),
}


def bootstrap(store: MemoryStore) -> list[str]:
    """Create any missing template files. Returns the virtual paths that were created."""
    created: list[str] = []
    for path, text in TEMPLATES.items():
        if store.read_text(path) is None and not store.known(path):
            store.write_text(path, text, actor=Actor.SYSTEM)
            created.append(path)
    return created
