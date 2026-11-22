# Contributing

Use GitHub Issues to discuss a proposed skill or report a problem. Submit changes through a pull request with a clear description of the behavior and the checks performed.

## Adding a skill

1. Place the complete skill under `skills/<skill-name>/`, following the [directory guide](skills/README.md).
2. Include `SKILL.md` with YAML `name` and `description`, a useful README and the applicable license. The folder and skill name should match.
3. Keep resources self-contained and use relative paths. Document required runtimes, tools, permissions and supported agents accurately.
4. Remove personal-project assumptions, machine-specific paths, credentials, private records and generated files. Review the proposed Git history as well as the final files.
5. Verify the skill from an independent copy in a different location. Follow the documented setup and example, inspect the actual output and record limitations.
6. Add the skill to the collection README with a relative link to its documentation.

Preserve existing copyright and license notices. Identify third-party material and its license; do not assume the collection's MIT license replaces a skill's own terms.

## Changing a skill

Keep the change focused and preserve unrelated behavior. Run the checks appropriate to that skill and describe the results in the pull request. When behavior, dependencies or installation changes, update the corresponding documentation.

## Reporting problems

Include the skill name, agent and version, operating system/runtime, reproduction steps, expected result and actual result. Review logs before sharing them and remove credentials or personal information.
