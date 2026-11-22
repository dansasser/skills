#!/usr/bin/env python3
"""Validate standard skill metadata, self-contained resources and collection links."""
from pathlib import Path
import argparse
import os
import re
import sys
from urllib.parse import unquote, urlsplit

from skills_ref import read_properties, validate

GENERATED = {'.git', '__pycache__', '.cache', '.pytest_cache', '.mypy_cache',
             '.ruff_cache', 'node_modules', 'workspace', 'out'}
OWNER_PATH = re.compile(r'/(?:home|Users)/[A-Za-z0-9_.-]+/|/(?:root)/[^\s]+|[A-Za-z]:[\\/]Users[\\/][^\s]+')
CREDENTIAL = re.compile(r'gh[pousr]_[A-Za-z0-9]{30,}|sk-proj-[A-Za-z0-9_-]{20,}|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----')
LINK = re.compile(r'!?\[[^\]]*\]\(([^)\n]+)\)')


def source_files(root):
    """Include untracked source and links; exclude only generated directories."""
    for directory, dirs, files in os.walk(root, followlinks=False):
        keep = []
        for name in dirs:
            p = Path(directory) / name
            if p.is_symlink():
                yield p
            elif name not in GENERATED and not name.startswith('.venv'):
                keep.append(name)
        dirs[:] = keep
        for name in files:
            if not name.endswith(('.pyc', '.pyo')):
                yield Path(directory) / name


def anchors(text):
    text = re.sub(r'```.*?```', '', text, flags=re.S)
    seen, result = {}, set()
    for title in re.findall(r'^#{1,6}\s+(.+?)\s*#*$', text, re.M):
        slug = re.sub(r'[^\w\- ]', '', title.lower().replace('`', '')).replace(' ', '-')
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        result.add(slug + ('-' + str(count) if count else ''))
    return result


def validate_repository(root):
    root = Path(root).resolve()
    errors = []
    skill_dir = root / 'skills'
    skills = sorted(p for p in skill_dir.iterdir() if p.is_dir() and not p.is_symlink()) if skill_dir.is_dir() else []
    if not skills:
        errors.append('No skills found under skills/.')
    catalog = root / 'README.md'
    catalog_text = catalog.read_text() if catalog.is_file() else ''
    for skill in skills:
        label = skill.relative_to(root)
        if not (skill / 'SKILL.md').is_file():
            errors.append(f'{label}: missing SKILL.md')
            continue
        try:
            errors.extend(f'{label}: {problem}' for problem in validate(skill))
            props = read_properties(skill)
            if not props.license:
                errors.append(f'{label}: declare license in SKILL.md')
        except Exception as exc:
            errors.append(f'{label}: invalid metadata: {exc}')
        for name in ('README.md', 'LICENSE'):
            if not (skill / name).is_file():
                errors.append(f'{label}: missing {name}')
        if f'skills/{skill.name}/README.md' not in catalog_text:
            errors.append(f'{label}: missing README catalog link')
    for path in source_files(root):
        label = path.relative_to(root)
        if path.is_symlink():
            errors.append(f'{label}: symlinked resources are not portable independent files')
            continue
        if path.name == '.env' or (path.name.startswith('.env.') and path.name != '.env.example'):
            errors.append(f'{label}: private local file must not be published')
        try:
            text = path.read_text(encoding='utf-8')
        except (UnicodeError, OSError):
            continue
        if OWNER_PATH.search(text):
            errors.append(f'{label}: machine-specific absolute owner path')
        if CREDENTIAL.search(text):
            errors.append(f'{label}: possible credential or private key')
        if path.suffix != '.md':
            continue
        boundary = next((s for s in skills if path.is_relative_to(s)), root)
        for raw in LINK.findall(text):
            target = unquote(raw.strip().strip('<>'))
            if urlsplit(target).scheme:
                continue
            relative, _, fragment = target.partition('#')
            if relative.startswith(('/', '~')):
                errors.append(f'{label}: absolute file link {raw}')
                continue
            dest = (path.parent / relative).resolve() if relative else path
            if not dest.is_relative_to(boundary):
                errors.append(f'{label}: resource outside skill or repository: {raw}')
                continue
            if not dest.is_file():
                errors.append(f'{label}: missing link target {raw}')
            elif fragment and dest.suffix == '.md' and fragment not in anchors(dest.read_text()):
                errors.append(f'{label}: missing anchor {raw}')
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    errors = validate_repository(args.root)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    skills = list((args.root / 'skills').glob('*/SKILL.md'))
    print(f'Validated {len(skills)} skill(s): metadata, catalog, licenses, links and portability checks passed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
