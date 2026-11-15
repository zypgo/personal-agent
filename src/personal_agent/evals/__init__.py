"""`pa eval`: does the agent behave the way an always-on agent should?

Unit tests prove the code does what it says. They cannot say whether the *agent* (this model, these
instructions, this memory) stays quiet on a quiet day, says each piece of news once, treats a hostile
webhook as data, or keeps a progress file. Those are behaviours, so they are measured the way
Anthropic suggests: small scenarios, graded on effects rather than wording, repeated, with the cost
and the noise reported.

- `spec`: the scenario file format, loaded from data and checked when loaded.
- `bench`: plays a scenario against a throwaway home and records what happened.
- `checks`: the vocabulary of things a scenario may demand.
- `runner`: plays scenarios scripted (to check the checks) or against the real model.
- `report`: the results as Markdown and JSON, with secrets scrubbed.
"""
