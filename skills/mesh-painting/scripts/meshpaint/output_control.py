"""Directory-scoped export reservations backed by the inherited prefix flock."""
import errno
import fcntl
import json
import os
from pathlib import Path
import stat
import time
import uuid

from . import procfs

COORDINATOR_LOCK = '.meshpaint-outputs.lock'
COORDINATOR_LEDGER = '.meshpaint-outputs.json'
ROOT_MARKER = '.meshpaint-generation-root.json'
MAX_LEASES = 64
MAX_LEDGER_BYTES = 16*1024*1024


def planned_exports(prefix, grid, stage='paint'):
    parent, name = os.path.split(os.fspath(prefix))
    if name in ('', '.', '..'):
        raise ValueError('--out must include a filename prefix')
    root = Path(parent or '.').resolve()
    suffixes = dict(preview=('_preview.png',), render=('_render.png',),
                    paint=('.png', '_full.png', '_partial.png', '_render.png'))[stage]
    names = {name+s for s in ('.pid', '.lock', '_usage.json', '_manifest.json', *suffixes)}
    if grid != '0' and stage != 'preview':
        cols, rows = map(int, grid.split('x'))
        for row in range(1, rows+1):
            for col in range(1, cols+1):
                base = name+'_render' if stage == 'render' else name
                names.add(f'{base}_tile_r{row}c{col}.png')
    return root, name, names


def refuse_source_collisions(scene, snapshot, prefix, grid, label='scene source', stage='paint'):
    root, name, names = planned_exports(prefix, grid, stage)
    source = Path(scene).resolve()
    if names.intersection({COORDINATOR_LOCK, COORDINATOR_LEDGER}):
        raise ValueError('output/control destinations collide with the directory coordinator')
    generated = root/(name+'_generations')
    if source == generated.resolve() or source.is_relative_to(generated.resolve()):
        raise ValueError(label+' collides with this prefix generation directory')
    identity = snapshot['device'], snapshot['inode']
    for target in [root/n for n in names | {COORDINATOR_LOCK, COORDINATOR_LEDGER}]:
        if source == target.resolve():
            raise ValueError(f'{label} collides with output/control destination {target.name}')
        try:
            entry = target.stat()
        except FileNotFoundError:
            continue
        if (entry.st_dev, entry.st_ino) == identity:
            raise ValueError(f'{label} inode collides with output/control destination {target.name}')
    for ancestor in (root, *root.parents):
        marker = ancestor/ROOT_MARKER
        if marker.is_file():
            raise ValueError('output prefix collides with an engine generation/control directory')
    return root, name, names


def _owned(entry):
    if not stat.S_ISREG(entry.st_mode) or entry.st_uid != os.getuid() or entry.st_nlink != 1:
        raise OSError('output coordinator entries must be owned regular files with one link')


def _open(directory, name, flags):
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=directory)
    try:
        _owned(os.fstat(fd))
    except BaseException:
        os.close(fd); raise
    return fd


def _read(directory):
    try:
        fd = _open(directory, COORDINATOR_LEDGER, os.O_RDONLY)
    except FileNotFoundError:
        return []
    with os.fdopen(fd, 'rb') as stream:
        raw = stream.read(MAX_LEDGER_BYTES+1)
    if len(raw) > MAX_LEDGER_BYTES:
        raise OSError('output coordinator ledger exceeds its metadata bound')
    try:
        value = json.loads(raw)
        entries = value['leases']
        if value.get('schema_version') != 1 or not isinstance(entries, list) or len(entries) > MAX_LEASES:
            raise ValueError()
        for entry in entries:
            if (not isinstance(entry, dict) or not isinstance(entry.get('prefix'), str) or
                    Path(entry['prefix']).name != entry['prefix'] or entry['prefix'] in ('', '.', '..') or
                    not isinstance(entry.get('run_id'), str) or not isinstance(entry.get('names'), list) or
                    len(entry['names']) > 2057 or not all(isinstance(n, str) and Path(n).name == n for n in entry['names']) or
                    any(not isinstance(entry.get(k), int) for k in ('pid', 'lock_device', 'lock_inode')) or
                    not isinstance(entry.get('started'), str)):
                raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise OSError('output coordinator ledger is malformed; refusing to replace unknown state') from None
    return entries


def _write(directory, entries):
    payload = json.dumps(dict(schema_version=1, leases=entries), separators=(',', ':')).encode()
    if len(payload) > MAX_LEDGER_BYTES or len(entries) > MAX_LEASES:
        raise OSError('output coordinator capacity exceeded')
    name = COORDINATOR_LEDGER+'.part-'+uuid.uuid4().hex
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, COORDINATOR_LEDGER, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try: os.unlink(name, dir_fd=directory)
        except FileNotFoundError: pass


def _owner_live(entry):
    try:
        fields = (procfs.mount()/str(entry['pid'])/'stat').read_bytes().rsplit(b')', 1)[1].split()
        return fields[0] != b'Z' and fields[19].decode() == entry['started']
    except (OSError, IndexError):
        return False


def _held(directory, entry):
    try:
        fd = _open(directory, entry['prefix']+'.lock', os.O_RDONLY)
    except OSError:
        return True                            # unknown/replaced lease state remains reserved
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (entry['lock_device'], entry['lock_inode']):
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True                        # includes a dead launcher's inherited supervisor lease
        return False
    finally:
        os.close(fd)


class ExportLease:
    def __init__(self, prefix, grid, run_id, prefix_fd, pid, started, stage='paint'):
        self.root, self.prefix, names = planned_exports(prefix, grid, stage)
        self.run_id = run_id
        info = os.fstat(prefix_fd)
        self.entry = dict(prefix=self.prefix, run_id=run_id, names=sorted(names), pid=pid,
                          started=started or '', lock_device=info.st_dev, lock_inode=info.st_ino)
        self.claimed = False
        self._transaction(claim=True)

    def _transaction(self, claim):
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        coordination = None
        try:
            coordination = _open(directory, COORDINATOR_LOCK, os.O_RDWR | os.O_CREAT)
            deadline = time.monotonic()+5
            while True:
                try:
                    fcntl.flock(coordination, fcntl.LOCK_EX | fcntl.LOCK_NB); break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise OSError('output directory coordinator did not become available within5seconds')
                    time.sleep(.01)
            entries = []
            for old in _read(directory):
                own_inode = (old['prefix'] == self.prefix and
                             (old['lock_device'], old['lock_inode']) ==
                             (self.entry['lock_device'], self.entry['lock_inode']))
                if own_inode or not _held(directory, old):
                    continue
                old['owner_live'] = _owner_live(old)
                entries.append(old)
            if claim:
                for old in entries:
                    overlap = set(old['names']).intersection(self.entry['names'])
                    if overlap:
                        raise OSError('output destinations overlap an active prefix '+old['prefix']+': '+sorted(overlap)[0])
                entries.append(self.entry)
            _write(directory, entries)
            self.claimed = claim
        finally:
            if coordination is not None: os.close(coordination)
            os.close(directory)

    def release(self):
        if self.claimed:
            self._transaction(claim=False)
