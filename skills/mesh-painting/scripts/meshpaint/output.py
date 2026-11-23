"""Complete immutable image generations selected by one atomic manifest."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import tempfile
import uuid

from . import limits
from .generation_store import GenerationStore, MAX_METADATA_BYTES

MAX_TILE_COUNT = 1024                   # bounded file count, qualified independently of pixel allocation


def digest(path):
    result = hashlib.sha256()
    with open(path, 'rb') as stream:
        for data in iter(lambda: stream.read(1 << 20), b''):
            result.update(data)
    return result.hexdigest()


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, value):
    """Replace a JSON entry without following a destination link."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=path.name+'.part-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_dir(path.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def png_size(path):
    with open(path, 'rb') as stream:
        head = stream.read(24)
    if len(head) != 24 or head[:8] != b'\x89PNG\r\n\x1a\n' or head[12:16] != b'IHDR':
        raise ValueError('output is not a complete PNG header')
    return list(struct.unpack('>II', head[16:24]))


def read_manifest(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd) as stream:
        entry = os.fstat(stream.fileno())
        if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1 or entry.st_uid != os.getuid():
            raise OSError('the output manifest must be an owned regular file with one link')
        if entry.st_size > MAX_METADATA_BYTES:
            raise ValueError('output manifest exceeds16MiB metadata bound')
        data = json.load(stream)
    if (not isinstance(data, dict) or data.get('schema_version') != 1 or
            not isinstance(data.get('run_id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', data['run_id']) or
            not isinstance(data.get('generation_id'), str) or not re.fullmatch(r'[a-f0-9]{32}', data['generation_id']) or
            not isinstance(data.get('images'), list) or len(data['images']) > 4099):
        raise ValueError('output manifest has an unsupported or malformed generation schema')
    for image in data['images']:
        if not isinstance(image, dict) or any(not isinstance(image.get(k), str) for k in ('export', 'path', 'sha256')):
            raise ValueError('output manifest has a malformed image entry')
        if any(Path(image[k]).is_absolute() or '..' in Path(image[k]).parts for k in ('export', 'path')):
            raise ValueError('output manifest image paths must be relative within the output directory')
    return data


def _directory(path):
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    entry = os.lstat(path)
    if not stat.S_ISDIR(entry.st_mode) or entry.st_uid != os.getuid():
        raise OSError('output generations must be owned directories, never links')


class Outputs:
    """Stage PNGs, preserve a complete generation, then select it atomically.

    Flat files are compatibility exports. A failed export may leave them stale;
    the manifest continues to identify the prior complete authoritative set.
    """

    def __init__(self, tell=None, *, prefix=None, run_id=None, stage=None):
        self.tag = f'.part-{os.getpid()}'
        self.pending, self.done = [], []
        self.tell = tell or (lambda **msg: None)
        self.prefix = prefix
        self.run_id = run_id or uuid.uuid4().hex
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', self.run_id):
            raise ValueError('invalid run identity for output generation')
        self.stage = stage
        self.provenance = {}
        self.current = {}
        self.receipt = None
        self.export_errors = []
        self._saved_sizes = {}
        self.store = None
        self.disk_report = {}
        self.observed_peak_staging_bytes = 0
        self.staging_bytes = 0

    def prepare(self):
        if self.store is None:
            self.store = GenerationStore(self.prefix, self.run_id, limits.active().level,
                                          read_manifest, atomic_json, digest, self.provenance)
        try:
            self.disk_report = self.store.prepare()
        except (OSError, ValueError, KeyError) as error:
            self.disk_report = dict(self.store.report, failure=str(error),
                                    blocked_entries=list(self.store.blocked), unknown_entries=list(self.store.unknown))
            self.tell(disk_storage=self.disk_report)
            raise
        self.tell(disk_storage=self.disk_report)
        return self.disk_report

    def save(self, image, path):
        path = os.fspath(path)
        if self.prefix is None:
            self.prefix = os.path.splitext(path)[0]
        if Path(path).parent.resolve() != Path(self.prefix).parent.resolve():
            raise ValueError('all output images must share the output-prefix directory')
        self.prepare()
        if len(set(self.current)|set(self.pending)|{path}) > MAX_TILE_COUNT+3:
            raise OSError('generation image count exceeds its finite output bound')
        temporary = path+self.tag
        try:
            with open(temporary, 'xb') as stream:     # O_EXCL refuses links and preserves descriptor identity
                entry = os.fstat(stream.fileno())
                if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1 or entry.st_uid != os.getuid():
                    raise OSError('image staging entry must be an owned regular file with one link')
                image.save(stream, format='PNG')
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                # An already-existing entry was never ours to remove.
                if 'stream' in locals():
                    os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        self._saved_sizes[path] = list(image.size)
        self.pending.append(path)
        self.staging_bytes += os.stat(temporary, follow_symlinks=False).st_size
        self.observed_peak_staging_bytes = max(self.observed_peak_staging_bytes, self.staging_bytes)
        return path

    def commit(self, *, terminal_reason=None, partial=False):
        if not self.pending:
            return self.receipt
        self.prepare()
        root = Path(self.prefix).parent
        generation_id = uuid.uuid4().hex
        staged = dict(self.current)
        for path in self.pending:
            staged[path] = dict(source=path+self.tag, size=self._saved_sizes[path])
        previous_good = self.store.previous_good(self.store.selected())
        generation = self.store.begin(generation_id, [Path(p).name for p in staged], previous_good)
        entries = []
        for export, data in staged.items():
            destination = generation/Path(export).name
            fd = os.open(data['source'], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            with os.fdopen(fd, 'rb') as source, open(destination, 'xb') as target:
                entry = os.fstat(source.fileno())
                if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1 or entry.st_uid != os.getuid():
                    raise OSError('output source must be an owned regular file with one link')
                from PIL import Image
                with Image.open(source) as decoded:
                    if list(decoded.size) != data['size']:
                        raise ValueError('encoded image dimensions do not match the staged output')
                    decoded.verify()
                source.seek(0)
                shutil.copyfileobj(source, target, length=1 << 20)
                target.flush()
                os.fsync(target.fileno())
            entries.append(dict(export=os.path.relpath(export, root), path=os.path.relpath(destination, root),
                                sha256=digest(destination), bytes=destination.stat().st_size, size=data['size']))
        _sync_dir(generation)
        terminal_reason = terminal_reason or 'preset_complete'
        manifest = dict(schema_version=1, run_id=self.run_id, generation_id=generation_id,
                        stage=self.stage, partial=partial, terminal_reason=terminal_reason,
                        provenance=self.provenance, images=entries)
        pointer = self.store.descriptor(previous_good) if previous_good else None
        manifest['previous_generation'] = manifest['previous_good_generation'] = pointer
        manifest['storage_plan'] = dict(maximum_retained_generations=2,
            maximum_generation_bytes=self.store.maximum_bytes,
            estimated_new_generation_bytes=self.store.estimated_new_bytes,
            maximum_metadata_bytes=MAX_METADATA_BYTES)
        self.store.seal(generation, manifest)
        limits.hold()
        try:
            while self.pending:
                path = self.pending[0]
                try:
                    os.replace(path+self.tag, path)
                except OSError as error:
                    self.export_errors.append(dict(path=path, error=str(error)))
                    self.tell(export_error=self.export_errors[-1])
                    raise
                self.done.append(self.pending.pop(0))
                self.tell(image=path)
            self.staging_bytes = 0
            _sync_dir(root)
            manifest_path = self.prefix+'_manifest.json'
            atomic_json(manifest_path, manifest)
            self.current = {str(root/e['export']):dict(source=str(root/e['path']), size=e['size']) for e in entries}
            self.disk_report = self.store.published(manifest)
            self.disk_report.update(staging_bytes=0, observed_peak_staging_bytes=self.observed_peak_staging_bytes)
            self.tell(disk_storage=self.disk_report)
            self.receipt = dict(run_id=self.run_id, generation_id=generation_id, stage=self.stage,
                                manifest=manifest_path, manifest_sha256=digest(manifest_path),
                                partial=partial, terminal_reason=terminal_reason, images=entries,
                                previous_generation=pointer, previous_good_generation=pointer)
            self.tell(output_generation=self.receipt)
            return self.receipt
        finally:
            limits.release()

    def discard(self):
        for path in self.pending:
            try:
                os.remove(path+self.tag)
            except FileNotFoundError:
                pass
        self.pending = []
        self.staging_bytes = 0
        if self.store is not None and self.store.prepared:
            try:
                self.disk_report = self.store.discard()
            except (OSError, ValueError, KeyError) as error:
                self.disk_report = dict(self.store.report, failure=str(error),
                                        blocked_entries=list(self.store.blocked), unknown_entries=list(self.store.unknown))
            self.disk_report.update(staging_bytes=0, observed_peak_staging_bytes=self.observed_peak_staging_bytes)
            self.tell(disk_storage=self.disk_report)


def verify_receipt(receipt, *, run_id, stage, prefix):
    """Supervisor-side output checks; does not import rendering dependencies."""
    if not isinstance(receipt, dict) or receipt.get('run_id') != run_id or receipt.get('stage') != stage:
        raise ValueError('the renderer sent no valid stage completion receipt')
    if receipt.get('partial') or receipt.get('terminal_reason') not in ('preset_complete', 'quality_target', 'diminishing_returns'):
        raise ValueError('the renderer did not finish a complete stage')
    manifest_path = prefix+'_manifest.json'
    if receipt.get('manifest') != manifest_path or digest(manifest_path) != receipt.get('manifest_sha256'):
        raise ValueError('completion receipt does not match the selected output manifest')
    manifest = read_manifest(manifest_path)
    if (manifest.get('schema_version') != 1 or manifest.get('run_id') != run_id or
            manifest.get('generation_id') != receipt.get('generation_id') or manifest.get('stage') != stage or
            manifest.get('partial') or manifest.get('terminal_reason') != receipt.get('terminal_reason')):
        raise ValueError('selected manifest belongs to a different run or generation')
    dimensions = receipt.get('dimensions') or {}
    delivery = list(dimensions.get('delivery_size', []))
    working = list(dimensions.get('working_size', []))
    if len(delivery) != 2 or len(working) != 2 or any(not isinstance(x, int) or x <= 0 for x in delivery+working):
        raise ValueError('completion receipt has no valid resolved dimensions')
    required = {'preview': {prefix+'_preview.png':working}, 'render': {prefix+'_render.png':delivery},
                'paint': {prefix+'.png':delivery, prefix+'_full.png':working, prefix+'_render.png':delivery}}[stage]
    grid = manifest.get('provenance', {}).get('settings', {}).get('tiles', '0')
    if stage != 'preview' and grid != '0':
        cols, rows = map(int, grid.split('x'))
        if not 0 < cols <= working[0] or not 0 < rows <= working[1] or cols*rows > MAX_TILE_COUNT:
            raise ValueError('completion manifest contains an invalid tile grid')
        tile_prefix = prefix+'_render' if stage == 'render' else prefix
        for row in range(rows):
            for col in range(cols):
                required[f'{tile_prefix}_tile_r{row+1}c{col+1}.png'] = [
                    (col+1)*working[0]//cols-col*working[0]//cols,
                    (row+1)*working[1]//rows-row*working[1]//rows]
    root = Path(prefix).parent
    by_export = {str(root/e['export']):e for e in manifest['images']}
    if not all(path in by_export for path in required):
        raise ValueError('completion manifest is missing required stage outputs')
    if any(by_export[path]['size'] != size for path, size in required.items()):
        raise ValueError('completion image dimensions do not match the resolved job')
    for entry in manifest['images']:
        relative = Path(entry['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('output manifest contains an invalid relative image path')
        path = root/relative
        if digest(path) != entry['sha256'] or path.stat().st_size != entry['bytes'] or png_size(path) != entry['size']:
            raise ValueError('an authoritative output image does not match its completion receipt')
    return manifest
