# Skill directory

Published skills belong in individual folders:

```text
skills/
  <skill-name>/
    SKILL.md
    README.md
    LICENSE
    agents/       # Agent-specific metadata, when needed
    scripts/      # Executable helpers, when needed
    references/   # Supporting instructions, when needed
    assets/       # Examples and other resources, when needed
```

Each skill is independently usable. Keep resources inside its folder and resolve them relative to that folder. Include only the directories the skill needs.

The skill README should explain:

- What the skill does and when to use it.
- Supported agents, operating systems, runtimes and dependencies.
- How to install the complete folder and invoke the skill.
- A reproducible example and its expected output.
- Verification, known limitations and troubleshooting.
- License coverage for code, instructions and bundled assets.

Keep private development records, credentials, machine-specific paths and generated output out of published skill folders and Git history.

See the [contribution guide](../CONTRIBUTING.md) and [collection README](../README.md).
