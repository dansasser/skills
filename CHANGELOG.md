# Changelog

Skill versions are recorded separately using `<skill-name>-v<version>` tags. Each tag points to the collection revision containing that skill release.

## Unreleased — Mesh Painting 0.2.0 candidate

- Preserve `legacy-seven` as the default and add opt-in `adaptive-v1` painting with image/region-dependent work, versioned finish/configuration settings, deterministic decisions and distinct quality, diminishing-return and budget outcomes.
- Validate mesh, scene, helper, camera, callback and native-array inputs before expensive allocation or native calls. Separate delivery/working dimensions, compact sparse regions and account for auxiliary maps within existing profile ceilings.
- Clip near-plane crossings with interpolated attributes in camera, reflection and point-shadow views while retaining legacy reference behavior for matched ordinary scenes.
- Select verified immutable output generations through a manifest and stage receipt. Keep selected/prior-good managed history bounded, report disk use, preserve unknown files and coordinate overlapping exports without serializing disjoint jobs.
- Preserve scene/config sources against output/control aliases, strengthen process/run identity and report early exits, failed publication, interruption and unfinished checkpoints truthfully. Full-state resume remains unimplemented.
- Rewrite the general README and portable skill workflow; document actual API contracts, painting controls, resources, provenance, authoritative outputs and recovery. Keep examples explicitly labeled and preserve the existing gallery and MIT scope.

This entry describes the source candidate. It does not create a tag or publish a release. Existing `mesh-painting-v0.1.0` history remains unchanged.

## mesh-painting-v0.1.0 — 2026-11-22

- Publish Mesh Painting as the collection's first skill, including its editable lighthouse and moor scenes, gallery, renderer, resource supervisor and regression checks.
- Document installation from the shared repository with the Skills CLI and manual folder copies, runtime prerequisites, examples, limits and recovery.
- Include standard skill metadata for the MIT license and version 0.1.0, with Linux/Python compatibility documented in the installation guide.
- Add collection validation, regression checks and GitHub Actions for metadata/resources and the default mesh-painting suite on Python 3.10 and 3.14.
- Add installation/compatibility documentation and private vulnerability reporting guidance.

- Preserve clean interrupted outcomes when NumPy wraps a stop exception during dependency loading; keep genuine import failures visible and add deterministic import-boundary regression checks.
