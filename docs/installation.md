# Installing skills

Install an individual skill from its folder in this collection. A skill does not need its own Git repository.

## Skills CLI

The commands below use Skills CLI 1.9.0 and require Node.js 22.20 or newer, internet access and Git. Run them from the project where you want the skill available. `--copy` installs independent files.

### Codex

```sh
npx skills@1.9.0 add dansasser/skills --skill mesh-painting --agent codex --copy
```

The project skill directory is `.agents/skills/mesh-painting/`.

### Claude Code

```sh
npx skills@1.9.0 add dansasser/skills --skill mesh-painting --agent claude-code --copy
```

The project skill directory is `.claude/skills/mesh-painting/`.

Omit `--agent` to choose an agent interactively. Add `--global` for a user-wide installation; review the destination before replacing an existing skill.

List the available skills without installing:

```sh
npx skills@1.9.0 add dansasser/skills --list
```

Pin a published skill version (the tag below selects the original 0.1.0 release, not unreleased changes):

```sh
npx skills@1.9.0 add dansasser/skills@mesh-painting-v0.1.0 --skill mesh-painting --copy
```

See the [pinned upstream CLI documentation](https://github.com/vercel-labs/skills/blob/v1.9.0/docs/commands.md#skills-add) for options and supported agents. Other agent choices depend on their own discovery, tool and runtime support.

## Manual installation

Clone the collection outside your agent's skill directory:

```sh
git clone https://github.com/dansasser/skills.git
```

Copy the complete `skills/mesh-painting` folder from that checkout into your agent's supported project or user skill location. Preserve `SKILL.md`, scripts, references, assets and the license together. Do not place the entire collection repository inside a directory intended for one skill.

| Agent | Project location | User location | Explicit invocation |
| --- | --- | --- | --- |
| Codex | `.agents/skills/mesh-painting/` | `~/.agents/skills/mesh-painting/` | `$mesh-painting` |
| Claude Code | `.claude/skills/mesh-painting/` | `~/.claude/skills/mesh-painting/` | `/mesh-painting` |

Keep every supporting file with the skill; installing only `SKILL.md` omits its engine and resources. These conventions follow the [Codex skill documentation](https://learn.chatgpt.com/docs/build-skills) and [Claude Code skill documentation](https://code.claude.com/docs/en/skills).

## Runtime setup and verification

Installing a skill supplies instructions and supporting files. Install its declared runtime dependencies separately, following the skill README. Keep the environment, editable work and generated output in your project.

For example, after the Codex project installation:

```sh
python3 -m venv .venv
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
.venv/bin/python -m pip install -r .agents/skills/mesh-painting/requirements.txt
mkdir -p workspace
cp .agents/skills/mesh-painting/assets/scenes/lighthouse.py workspace/lighthouse.py
.venv/bin/python .agents/skills/mesh-painting/scripts/run.py workspace/lighthouse.py --look golden_hour --seed 11 --stage preview --out workspace/out/lighthouse
```

Open `workspace/out/lighthouse_preview.png`. Follow the [general mesh-painting workflow](../skills/mesh-painting/README.md#general-workflow) for full render and tile inspection, painting and delivery.

Default painting uses the preserved `legacy-seven` recipe. Image-dependent `adaptive-v1` painting is opt-in; follow the skill README for controls and honest completed/stopped outcomes. The environment and output-generation protocol are the same for either policy.

Start a fresh agent session in the project and invoke the skill explicitly. Installation/file verification and direct renderer output do not establish that a particular agent session discovered the skill or followed its visual-review workflow; verify those behaviors through normal use.

## Compatibility

| Skill | Agent-facing support | Execution requirements |
| --- | --- | --- |
| Mesh Painting | Standard Agent Skills instructions; Codex and Claude Code project-copy installation paths are documented | Linux, Python 3.10+, NumPy/SciPy/Pillow, `findmnt` and procfs; process forking, signals, locking and address-space limits; a PNG-viewing tool; optional C compiler |

Native Windows, macOS and hosted ChatGPT rendering have not been verified. A Linux execution environment must satisfy the requirements in the [skill README](../skills/mesh-painting/README.md#requirements). Agent Skills compatibility does not change those requirements.

## Updating or removing a skill

Preserve an existing installation and your working scenes before an update. Install a reviewed version through the same method and rerun the skill's example. Remove a skill through your installer's documented removal command or remove only its installed folder; keep user scenes and output outside that folder.
