"""Bounded, journal-owned image generations; unknown user entries are never pruned."""
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import uuid

from .output_control import ROOT_MARKER

JOURNAL = '.meshpaint-generations.json'
MAX_METADATA_BYTES = 16*1024*1024
MAX_RECORDS = 32
MAX_SCAN_ENTRIES = 8192


def _regular(path):
    entry = os.lstat(path)
    if not stat.S_ISREG(entry.st_mode) or entry.st_uid != os.getuid() or entry.st_nlink != 1:
        raise OSError('generation metadata/files must be owned regular single-link entries')
    return entry


def _read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as stream:
        entry = os.fstat(stream.fileno())
        if not stat.S_ISREG(entry.st_mode) or entry.st_uid != os.getuid() or entry.st_nlink != 1:
            raise OSError('generation metadata must be an owned regular single-link entry')
        raw = stream.read(MAX_METADATA_BYTES+1)
    if len(raw) > MAX_METADATA_BYTES:
        raise OSError('generation metadata exceeds16MiB')
    return json.loads(raw)


def _directory(path):
    try: os.mkdir(path, 0o700)
    except FileExistsError: pass
    entry = os.lstat(path)
    if not stat.S_ISDIR(entry.st_mode) or entry.st_uid != os.getuid():
        raise OSError('generation directories must be owned directories, never links')


def _sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try: os.fsync(fd)
    finally: os.close(fd)


class GenerationStore:
    def __init__(self, prefix, run_id, level, reader, writer, digest, context=None):
        self.prefix = os.fspath(prefix); self.run_id = run_id; self.level = level
        self.root = Path(self.prefix+'_generations'); self.parent = Path(prefix).parent
        self.read_manifest, self.write_json, self.digest = reader, writer, digest
        self.maximum_bytes = 20*int(level.pixels)+1027*4096+MAX_METADATA_BYTES
        context = context or {}; dimensions = context.get('dimensions') or {}
        working = dimensions.get('working_size', [int(level.pixels), 1])
        delivery = dimensions.get('delivery_size', [int(level.pixels), 1])
        a = int(working[0])*int(working[1]); d = int(delivery[0])*int(delivery[1])
        settings = context.get('settings') or {}; stage = settings.get('stage', 'paint')
        grid = settings.get('tiles', '0'); tile_count = 0 if grid == '0' else math.prod(map(int, grid.split('x')))
        pixels = a if stage == 'preview' else a+d if stage == 'render' else 2*(a+d)
        files = 1 if stage == 'preview' else tile_count+(1 if stage == 'render' else 3)
        self.estimated_new_bytes = min(self.maximum_bytes, 5*pixels+files*4096+MAX_METADATA_BYTES)
        self.records = {}; self.removed = []; self.unknown = []; self.blocked = []
        self.prepared = False; self.active = None; self.report = {}
        self.journal_staging = None

    def _persist(self):
        value = dict(schema_version=1, prefix=Path(self.prefix).name, generations=list(self.records.values()))
        if len(json.dumps(value).encode()) > MAX_METADATA_BYTES or len(self.records) > MAX_RECORDS:
            raise OSError('generation ownership journal exceeds its metadata bound')
        temporary = self.root/self.journal_staging
        try:
            _regular(temporary)
        except FileNotFoundError: pass
        else: os.unlink(temporary)                  # exact creation slot is bound by the root ownership record
        with open(temporary, 'xb') as stream:
            stream.write(json.dumps(value, separators=(',', ':')).encode())
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, self.root/JOURNAL); _sync(self.root)

    def _load(self):
        try: value = _read(self.root/JOURNAL)
        except FileNotFoundError: return
        if value.get('schema_version') != 1 or value.get('prefix') != Path(self.prefix).name:
            raise OSError('unknown generation ownership journal; refusing to replace it')
        records = value.get('generations')
        if not isinstance(records, list) or len(records) > MAX_RECORDS:
            raise OSError('generation journal exceeds its record bound')
        for record in records:
            if (not isinstance(record, dict) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', record.get('run_id', '')) or
                    not re.fullmatch(r'[a-f0-9]{32}', record.get('generation_id', '')) or
                    record.get('directory') != record['run_id']+'/'+record['generation_id'] or
                    record.get('state') not in ('preparing', 'sealed', 'published') or
                    not isinstance(record.get('files'), list) or len(record['files']) > 1028 or
                    not all(isinstance(n, str) and Path(n).name == n and n not in ('', '.', '..') for n in record['files'])):
                raise OSError('malformed generation creation record; refusing unknown state')
            self.records[record['generation_id']] = record

    def selected(self):
        try: return self.read_manifest(self.prefix+'_manifest.json')
        except FileNotFoundError: return None

    def descriptor(self, manifest):
        path = self.root/manifest['run_id']/manifest['generation_id']/'manifest.json'
        return dict(run_id=manifest['run_id'], generation_id=manifest['generation_id'],
                    manifest=os.path.relpath(path, self.parent))

    def _good(self, manifest):
        if not manifest or manifest.get('partial') or manifest.get('terminal_reason') not in (
                'preset_complete', 'quality_target', 'diminishing_returns'):
            return False
        stage = manifest.get('stage'); name = Path(self.prefix).name
        required = dict(preview=[name+'_preview.png'], render=[name+'_render.png'],
                        paint=[name+'.png', name+'_full.png', name+'_render.png']).get(stage)
        if not required: return False
        exports = {x['export'] for x in manifest['images']}
        if not set(required) <= exports: return False
        grid = manifest.get('provenance', {}).get('settings', {}).get('tiles', '0')
        if stage != 'preview' and grid != '0':
            cols, rows = map(int, grid.split('x'))
            if min(cols, rows) < 1 or cols*rows > 1024: return False
            tile_prefix = name+'_render' if stage == 'render' else name
            if not {f'{tile_prefix}_tile_r{r}c{c}.png' for r in range(1, rows+1) for c in range(1, cols+1)} <= exports:
                return False
        return True

    def previous_good(self, selected):
        if self._good(selected): return selected
        return self._prior_good(selected)

    def _prior_good(self, selected):
        if not selected: return None
        pointer = selected.get('previous_good_generation') or selected.get('previous_generation')
        visited = set()
        for _ in range(MAX_RECORDS):
            if not pointer: return None
            if pointer['generation_id'] in visited: raise OSError('prior-good generation chain is cyclic')
            visited.add(pointer['generation_id'])
            path = Path(pointer['manifest'])
            if path.is_absolute() or '..' in path.parts: raise OSError('invalid prior-good generation pointer')
            prior = self.read_manifest(self.parent/path)
            if prior['generation_id'] != pointer['generation_id']:
                raise OSError('prior-good generation identity changed')
            self._verify(prior)
            if self._good(prior): return prior
            pointer = prior.get('previous_good_generation') or prior.get('previous_generation')
        raise OSError('prior-good generation chain exceeds its traversal bound')

    def _verify(self, manifest):
        for image in manifest['images']:
            path = self.parent/image['path']
            entry = _regular(path)
            if entry.st_size != image['bytes'] or self.digest(path) != image['sha256']:
                raise OSError('generation image no longer matches its recorded bytes')

    def _adopt_selected(self, manifest):
        if not manifest: return
        directory = self.root/manifest['run_id']/manifest['generation_id']
        own = directory/'manifest.json'
        value = self.read_manifest(own)
        if value != manifest: raise OSError('selected generation does not match its immutable manifest')
        self._verify(value)
        files = [Path(x['path']).name for x in value['images']]+['manifest.json']
        if manifest['generation_id'] in self.records:
            self.records[manifest['generation_id']]['state'] = 'published'
            return
        if {p.name for p in directory.iterdir()} != set(files):
            raise OSError('selected legacy generation contains unknown entries; refusing adoption')
        self.records[value['generation_id']] = dict(run_id=value['run_id'], generation_id=value['generation_id'],
            directory=value['run_id']+'/'+value['generation_id'], files=files, state='published',
            device=directory.stat().st_dev, inode=directory.stat().st_ino)

    def _entries(self, record):
        directory = self.root/record['directory']
        run = self.root/record['run_id']
        if run.exists():
            ancestor = os.lstat(run)
            if not stat.S_ISDIR(ancestor.st_mode) or ancestor.st_uid != os.getuid():
                raise OSError('generation run ancestor must be an owned directory, never a link')
        if not directory.exists(): return [], 0
        entry = os.lstat(directory)
        if not stat.S_ISDIR(entry.st_mode) or entry.st_uid != os.getuid():
            raise OSError('unknown generation directory type/owner')
        if 'device' in record and (entry.st_dev, entry.st_ino) != (record['device'], record['inode']):
            raise OSError('generation directory identity changed')
        paths = list(directory.iterdir())
        if len(paths) > 1028: raise OSError('generation directory contains too many entries')
        if any(p.name not in record['files'] for p in paths):
            raise OSError('generation contains unknown user entries')
        if 'device' not in record:
            # The only pre-binding crash window is immediately after mkdir: no file
            # is written until the inode binding has been persisted by begin().
            if record['state'] != 'preparing' or paths:
                raise OSError('unbound generation has contents; refusing unknown ownership')
            record.update(device=entry.st_dev, inode=entry.st_ino)
            self._persist()                         # validated adoption of the recorded empty creation slot
        size = sum(_regular(p).st_size for p in paths)
        if record['state'] != 'preparing':
            manifest = self.read_manifest(directory/'manifest.json')
            if manifest['generation_id'] != record['generation_id'] or manifest['run_id'] != record['run_id']:
                raise OSError('generation identity changed')
            self._verify(manifest)
        return paths, size

    def _remove(self, record):
        paths, _ = self._entries(record)
        directory = self.root/record['directory']
        # This flat, declared-file deletion never follows a nested user directory.
        root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        run_fd = generation_fd = None
        try:
            try:
                run_fd = os.open(record['run_id'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root_fd)
                try:
                    generation_fd = os.open(record['generation_id'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=run_fd)
                except FileNotFoundError: pass
                if generation_fd is not None:
                    info = os.fstat(generation_fd)
                    if 'device' in record and (info.st_dev, info.st_ino) != (record['device'], record['inode']):
                        raise OSError('generation directory changed before pruning')
                    for path in paths: os.unlink(path.name, dir_fd=generation_fd)
                    os.close(generation_fd); generation_fd = None
                    os.rmdir(record['generation_id'], dir_fd=run_fd)
                os.close(run_fd); run_fd = None
                try: os.rmdir(record['run_id'], dir_fd=root_fd)
                except OSError: pass                 # another retained generation or unknown user entries
            except FileNotFoundError: pass
        finally:
            if generation_fd is not None: os.close(generation_fd)
            if run_fd is not None: os.close(run_fd)
            os.close(root_fd)
        self.removed.append(record['generation_id'])
        self.records.pop(record['generation_id'], None)

    def _unknown(self):
        unknown = []; count = 0
        known_runs = {x['run_id'] for x in self.records.values()}
        known_dirs = {x['directory'] for x in self.records.values()}
        for path in self.root.iterdir():
            count += 1
            if count > MAX_SCAN_ENTRIES: raise OSError('output directory scan exceeds its entry bound')
            if path.name in (ROOT_MARKER, JOURNAL): continue
            if path.name == self.journal_staging:
                _regular(path); os.unlink(path)      # exact slot has an independent durable creation record
                continue
            if path.name in known_runs and path.is_dir() and not path.is_symlink():
                for child in path.iterdir():
                    count += 1
                    if count > MAX_SCAN_ENTRIES: raise OSError('output directory scan exceeds its entry bound')
                    if path.name+'/'+child.name not in known_dirs:
                        unknown.append(dict(path=str(child.relative_to(self.root)), kind='unknown generation/run entry'))
            else:
                unknown.append(dict(path=path.name, kind='unmanaged user entry'))
        self.unknown = unknown

    def _protect(self, selected):
        ids = {selected['generation_id']} if selected else set()
        good = self._prior_good(selected)
        if good: ids.add(good['generation_id'])
        return ids

    def _prune(self, protected, *, check_only=False):
        blocked = []
        for key, record in list(self.records.items()):
            if key in protected: continue
            try:
                self._entries(record)
                if not check_only: self._remove(record)
            except (OSError, ValueError, KeyError) as error:
                blocked.append(dict(generation_id=key, detail=str(error)))
        self.blocked = blocked
        if blocked: raise OSError('generation retention blocked by unknown/changed entries; previous authority preserved')

    def _report(self):
        retained = []; size = 0; incomplete = 0
        for record in self.records.values():
            try: _, used = self._entries(record)
            except (OSError, ValueError, KeyError) as error:
                self.unknown.append(dict(path=record['directory'], kind='changed managed generation', detail=str(error)))
                used = None
            if used is not None: size += used
            if record['state'] == 'preparing': incomplete += 1
            retained.append(dict(generation_id=record['generation_id'], run_id=record['run_id'],
                                 bytes=used, state=record['state']))
        control = 0
        for path in (self.root/ROOT_MARKER, self.root/JOURNAL):
            try: control += _regular(path).st_size
            except FileNotFoundError: pass
        self.report = dict(retained_generation_count=len(retained), retained_bytes=size,
            retained_generation_bytes=size, control_bytes=control, actual_store_bytes_known=size+control,
            retained_byte_count_complete=all(x['bytes'] is not None for x in retained),
            retained_generations=retained, incomplete_generation_count=incomplete,
            removed_ids=list(self.removed), unknown_entries=list(self.unknown), blocked_entries=list(self.blocked),
            maximum_retained_generations=2, maximum_generation_bytes=self.maximum_bytes,
            maximum_metadata_bytes=MAX_METADATA_BYTES, estimated_new_generation_bytes=self.estimated_new_bytes,
            estimated_additional_disk_bytes=2*self.estimated_new_bytes,
            disk_free_bytes=shutil.disk_usage(self.parent).free,
            unmanaged_bytes='not included; unknown user directories are preserved, not traversed or claimed bounded')
        return self.report

    def prepare(self):
        if self.prepared: return self.report
        _directory(self.root)
        marker = self.root/ROOT_MARKER
        value = dict(schema_version=1, kind='meshpaint-generation-root', prefix=Path(self.prefix).name)
        try:
            existing = _read(marker)
            if any(existing.get(k) != v for k, v in value.items()): raise OSError('unknown generation-root ownership marker')
            staging = existing.get('journal_staging')
            if not isinstance(staging, str) or not re.fullmatch(r'\.meshpaint-journal-stage-[a-f0-9]{32}', staging):
                raise OSError('generation-root journal staging ownership is not recorded')
            self.journal_staging = staging
        except FileNotFoundError:
            self.journal_staging = '.meshpaint-journal-stage-'+uuid.uuid4().hex
            value['journal_staging'] = self.journal_staging
            with open(marker, 'x') as stream:
                json.dump(value, stream); stream.flush(); os.fsync(stream.fileno())
            _sync(self.root)
        self._load()
        selected = self.selected(); self._adopt_selected(selected)
        good = self._prior_good(selected); self._adopt_selected(good)
        protected = self._protect(selected)
        self._prune(protected)
        self._unknown(); self._persist(); self.prepared = True
        report = self._report()
        if any(x['bytes'] is None for x in report['retained_generations']):
            raise OSError('protected generations contain unknown/changed entries; bounded admission cannot be proved')
        if report['retained_generation_count'] > 2 or report['retained_bytes'] > 2*self.maximum_bytes:
            raise OSError('protected generation history exceeds the selected profile disk bound')
        if report['disk_free_bytes'] < 2*self.estimated_new_bytes+16*1024*1024:
            raise OSError('insufficient free disk for bounded image staging, generation and recovery reserve')
        return report

    def begin(self, generation_id, file_names, previous_good):
        self.prepare()
        if len(file_names) > 1027: raise OSError('generation image count exceeds its finite bound')
        protected = {previous_good['generation_id']} if previous_good else set()
        # Before changing authority, prove every generation that would become obsolete is safely prunable.
        selected = self.selected()
        if selected and selected['generation_id'] in self.records: protected.add(selected['generation_id'])
        self._prune(protected, check_only=True)
        prospective = {previous_good['generation_id']} if previous_good else set()
        self._prune(prospective, check_only=True)
        if shutil.disk_usage(self.parent).free < self.estimated_new_bytes+16*1024*1024:
            raise OSError('insufficient free disk for a complete generation and output reserve')
        record = dict(run_id=self.run_id, generation_id=generation_id,
                      directory=self.run_id+'/'+generation_id, files=sorted(set(file_names)|{'manifest.json'}),
                      state='preparing')
        self.records[generation_id] = record
        self._persist()                              # durable creation record precedes either directory
        run = self.root/self.run_id; _directory(run)
        directory = run/generation_id
        try: os.mkdir(directory, 0o700)
        except FileExistsError:
            self.records.pop(generation_id); self._persist()
            raise OSError('generation creation collided with an existing unknown directory') from None
        _sync(run); _sync(self.root)
        entry = os.lstat(directory)
        record.update(device=entry.st_dev, inode=entry.st_ino); self._persist()
        self.active = generation_id
        return directory

    def seal(self, generation, manifest):
        payload = json.dumps(manifest, indent=2, allow_nan=False)+'\n'
        if len(payload.encode()) > MAX_METADATA_BYTES:
            raise OSError('output manifest/trace exceeds16MiB metadata bound')
        with open(generation/'manifest.json', 'x') as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        _sync(generation)
        record = self.records[manifest['generation_id']]
        _, used = self._entries(record)
        if used > self.maximum_bytes: raise OSError('encoded generation exceeds selected profile disk bound')
        record['state'] = 'sealed'; self._persist()

    def published(self, manifest):
        self.records[manifest['generation_id']]['state'] = 'published'
        self._persist()
        self.active = None
        self._prune(self._protect(manifest)); self._unknown(); self._persist()
        return self._report()

    def discard(self):
        if not self.prepared: return self.report
        selected = self.selected()
        protected = self._protect(selected)
        self._prune(protected); self._unknown(); self._persist()
        self.active = None
        return self._report()
