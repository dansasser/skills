# Changelog

Skill versions are recorded separately using `<skill-name>-v<version>` tags. Each tag points to the collection revision containing that skill release.

## mesh-painting-v0.1.0 — 2026-11-22

- Publish Mesh Painting as the collection's first skill, including its editable lighthouse and moor scenes, gallery, renderer, resource supervisor and regression checks.
- Document installation from the shared repository with the Skills CLI and manual folder copies, runtime prerequisites, examples, limits and recovery.
- Include standard skill metadata for the MIT license and version 0.1.0, with Linux/Python compatibility documented in the installation guide.
- Add collection validation, regression checks and GitHub Actions for metadata/resources and the default mesh-painting suite on Python 3.10 and 3.14.
- Add installation/compatibility documentation and private vulnerability reporting guidance.

- Preserve clean interrupted outcomes when NumPy wraps a stop exception during dependency loading; keep genuine import failures visible and add deterministic import-boundary regression checks.
