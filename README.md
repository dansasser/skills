# Skills

Reusable agent skills published by **Dan Sasser**. Each skill is a self-contained folder with standard `SKILL.md` metadata, instructions, supporting resources and its own license.

## Available skills

| Skill | What it does | Runtime | License |
| --- | --- | --- | --- |
| [Mesh Painting](skills/mesh-painting/README.md) | Paint editable landscapes, coasts and buildings with triangle meshes and brushstrokes, without an image-generation model | Linux, Python 3.10+, NumPy, SciPy, Pillow; optional C compiler | [MIT](skills/mesh-painting/LICENSE) |
| [Graph Development](skills/graph-development/README.md) | Build and operate bounded software-development lifecycle graphs with specification, approval, execution and recovery lineage | Linux, Python 3.12+; standard library | [Apache-2.0](skills/graph-development/LICENSE) |
| [Video Analysis](skills/video-analysis/README.md) | Examine a cut or compare reels to identify recurring editing choices, with timestamps for the visual and audio evidence | Linux, Python 3, FFmpeg and ffprobe. Optional whisper.cpp for transcription | [MIT](skills/video-analysis/LICENSE) |

## Install a skill

With Node.js 22.20 or newer, choose a skill using the tested Skills CLI:

```sh
npx skills@1.9.0 add dansasser/skills --skill mesh-painting --copy

# Or select Graph Development:
npx skills@1.9.0 add dansasser/skills --skill graph-development --copy
```

Choose the agent you use when prompted. This installs the selected skill as independent files. Follow the skill's README to install its runtime dependencies and run an example.

See the [installation guide](docs/installation.md) for Codex and Claude Code commands, version pinning, manual installation, compatibility and verification. Each skill documents its own requirements; the Agent Skills format alone does not supply a rendering runtime or guarantee every agent can execute it.

## Verify the collection

Collection validation uses Python 3.11 or newer and the official Agent Skills reference validator, pinned in `tools/requirements.txt`:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r tools/requirements.txt
.venv/bin/python tools/validate_collection.py
.venv/bin/python -m unittest discover -s tests -v
```

These checks validate skill metadata, the catalog, licenses, relative links and anchors, self-contained resources and common portability/privacy mistakes. They supplement a full source/history review. Each skill also supplies its own runtime checks; see [Mesh Painting verification](skills/mesh-painting/README.md#verification) and [Graph Development verification](skills/graph-development/README.md#verification).

The GitHub Actions workflow configures collection validation and mesh-painting's default engine checks on Python 3.10 and 3.14. Full slow checks remain available through the skill's documented command.

## Contributing and security

Read [CONTRIBUTING.md](CONTRIBUTING.md) and the [skill directory guide](skills/README.md). Report ordinary problems through [GitHub Issues](https://github.com/dansasser/skills/issues). Follow [SECURITY.md](SECURITY.md) for vulnerabilities.

## Versions and licensing

Skill releases use tags such as `mesh-painting-v0.1.0`; see [CHANGELOG.md](CHANGELOG.md). A tag identifies the collection revision containing that skill version.

Collection documentation and metadata use the [MIT License](LICENSE). Each skill includes its own license covering its code, instructions and bundled assets as stated in its README. Third-party components retain their own licenses.
