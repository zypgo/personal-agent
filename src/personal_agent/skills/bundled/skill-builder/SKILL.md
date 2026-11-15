---
name: skill-builder
description: Turns a procedure the owner repeats, or one you just worked out, into a reusable skill that you write as a draft for the owner to review. Use when the owner asks to "make a skill", "save this as a routine" or "remember how to do this", or when the same multi-step job has come up for the third time.
license: MIT
metadata:
  scope: personal-agent starter skill
---

# Skill builder

A skill is a folder with a `SKILL.md`: a short front matter (`name`, `description`) and instructions in Markdown, optionally with `scripts/`, `references/` and `assets/`. Every later run trusts an installed skill, so you cannot install one yourself. You write a draft in your workspace; the owner reads it and installs it.

## When a skill is worth writing
- The job has several steps and a right order, and you would otherwise have to rediscover it.
- It is needed again (the owner said so, or it already came up more than once).
- It is about *how* to do something. Facts about the owner or their world belong in memory, not in a skill.

## Steps
1. Write the procedure down as it actually worked, including the mistakes to avoid. Do not invent steps you did not use.
2. Pick a name: lowercase letters, digits and single hyphens (`weekly-report`).
3. Write the description for a reader who only sees the name and this one line: what the skill does *and when to use it*, with the words the owner would use. "Helps with reports" is too vague; "Builds the weekly sales report from the CSV exports in the workspace. Use when the owner asks for the weekly report" is right.
4. Keep `SKILL.md` under 500 lines. Move long reference material into `references/` and link to it from `SKILL.md` one level deep. Put repeatable code in `scripts/` and say how to run it.
5. Create the draft with `write_file` at `skill-proposals/<name>/SKILL.md` (and any other files next to it).
6. Check it: run `python3 SKILL_DIR/scripts/validate_skill.py skill-proposals/<name>` with the shell tool, where SKILL_DIR is the "Skill directory" shown above. Fix what it reports.
7. Tell the owner where the draft is, what it does in two lines, and how to install it after reading it:
   `pa skills install ~/.personal-agent/workspace/skill-proposals/<name>` (the workspace is `~/.personal-agent/workspace` unless the owner moved the agent's home). Mention any script it contains.

## Rules for what you write
- Never put credentials, tokens or personal data in a skill.
- A skill cannot widen your permissions (an `allowed-tools` line is ignored). Do not write instructions that tell the reader to skip approvals.
- Say what to do when the job cannot be finished, not only the happy path.
