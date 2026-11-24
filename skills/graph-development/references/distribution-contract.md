# Clean distribution and path contract

Graph Development distributions are location-independent. The executing skill
resolves bundled assets from the directory containing the skill. Project
artifacts resolve from an explicit project root when one is supplied; otherwise
the canonical graph file's directory is the authority for its relative
`project.root`.

## Serialized paths

Persisted paths use POSIX-style project-relative strings. They must not use:

- filesystem-root paths;
- drive-root paths;
- home-directory shortcuts;
- parent-directory traversal;
- NUL characters;
- backslash separators; or
- a symlink that resolves outside the declared authority.

Generated canonical graphs use `project.root` equal to `.`. Project instruction
files, runtime definition references, worker working directories, and evidence
records are serialized relative to their owning project or run directory.
Caller working directory is never a path authority.

Scaffolding therefore requires `--output-dir` to resolve to exactly the same
directory as `--project-root`. Nested graph output is refused instead of
serializing a parent traversal or making `project.root` describe the wrong
authority.

## Clean tree

`scripts/distribution_manifest.py` walks every distribution file, rejects
symlinks, and emits sorted relative file records with byte sizes and SHA-256
digests. Runtime state, version-control metadata, interpreter caches, tool
caches, evaluation result directories, and compiled Python files are excluded.

Text discovery is content-based rather than suffix-based. Every UTF-8 artifact
without NUL bytes is included, so extensionless notices and text files receive
the same portability checks as Markdown, JSON, YAML, and Python files.

From the executing skill directory:

```bash
python3 scripts/distribution_manifest.py
python3 scripts/distribution_manifest.py --text-artifacts
```

Both commands are read-only and produce deterministic JSON.

## Provider and network boundary

`scripts/verify_distribution.py` starts every release-gate stage with a
portable Python audit-hook guard that denies socket audit events. A controlled
canary must prove denial, each stage must prove the guard loaded, and any
recorded network attempt fails the gate. The provider-boundary stage separately
proves that ordinary core imports and workflows contain and load no concrete
provider tooling. Only after both controls pass may the report state zero
provider calls and no network use.
