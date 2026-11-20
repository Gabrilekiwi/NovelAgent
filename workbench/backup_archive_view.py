"""Checked, bounded archive access shared by offline evidence verifiers."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import zipfile

from workbench.backup_archive import BackupArchiveError, verify_archive, _member, _lineage
from workbench.backup_files import _ordinary, _identity


class ArchiveView:
    def __init__(self, verified, zipped):
        self.verified = verified
        self.manifest = verified['manifest']
        self._zipped = zipped
        self._reads = self._bytes = 0
        self._active = True

    def _entry(self, name):
        if not self._active:
            raise BackupArchiveError('归档读取上下文已结束。')
        _member(name)
        record = self.manifest['members'].get(name)
        if record is None:
            raise BackupArchiveError('归档所需成员缺失或被排除：' + name)
        entry = self._zipped.getinfo(name)
        if entry.file_size != record['size']:
            raise BackupArchiveError('归档成员大小偏离清单。')
        return entry, record

    def read_member(self, name):
        entry, record = self._entry(name)
        self._reads += 1
        self._bytes += record['size']
        if record['size'] > 32 * 1024**2 or self._reads > 20000 or self._bytes > 256 * 1024**2:
            raise BackupArchiveError('离线证据读取超过支持的上限。')
        raw = self._zipped.read(entry)
        if len(raw) != record['size'] or hashlib.sha256(raw).hexdigest() != record['sha256']:
            raise BackupArchiveError('归档成员内容偏离清单。')
        return raw

    def read_root(self, key, relative):
        _member(relative)
        if key not in self.manifest['roots']:
            raise BackupArchiveError('归档缺少所需来源根。')
        return self.read_member(self.manifest['roots'][key]['archive_path'] + '/' + relative)

    def copy_member(self, name, output):
        """Stream a single verified member to a caller-owned temporary stream."""
        entry, record = self._entry(name)
        checksum = hashlib.sha256(); length = 0
        with self._zipped.open(entry) as source:
            while chunk := source.read(1024**2):
                length += len(chunk)
                if length > record['size']:
                    raise BackupArchiveError('归档成员流超出声明大小。')
                checksum.update(chunk); output.write(chunk)
        if length != record['size'] or checksum.hexdigest() != record['sha256']:
            raise BackupArchiveError('归档成员流内容偏离清单。')


@contextmanager
def verified_archive_view(package):
    package = Path(package).absolute()
    verified = verify_archive(package)
    lineage = _lineage(package)
    archive = package / 'archive.zip'
    physical = _identity(_ordinary(archive))
    with archive.open('rb') as stream:
        if _identity(os.fstat(stream.fileno())) != physical:
            raise BackupArchiveError('离线读取的归档被替换。')
        with zipfile.ZipFile(stream) as zipped:
            view = ArchiveView(verified, zipped)
            try:
                yield view
                if _identity(os.fstat(stream.fileno())) != physical:
                    raise BackupArchiveError('离线读取期间归档变化。')
            finally:
                view._active = False
    if (_identity(_ordinary(archive)) != physical or _lineage(package) != lineage
            or verify_archive(package) != verified):
        raise BackupArchiveError('离线核验前后归档或完成凭据变化。')
