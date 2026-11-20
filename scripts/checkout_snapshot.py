"""Back up tracked checkout bytes, verifying Git filters separately from tar."""
import hashlib
import os
from pathlib import Path
import subprocess
import tarfile


def backup_checkout(root: Path, revision: str, destination: Path) -> int:
    """Git archive applies eol attributes; a filesystem snapshot must not.

    Validate each disk object against its blob (clean filters if needed), then
    write PAX from disk and compare every member byte-for-byte with that disk.
    Caller must ensure a clean checkout and exclude concurrent writers.
    """
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], timeout=180)

    entries = []
    for record in git("ls-tree", "-rz", revision).split(b"\0"):
        if not record:
            continue
        header, name = record.split(b"\t", 1)
        mode, kind, oid = header.split()
        path = os.fsdecode(name)
        if kind != b"blob":
            raise ValueError(f"Unsupported snapshot object: {path}")
        disk = root / path
        data = os.fsencode(os.readlink(disk)) if mode == b"120000" else disk.read_bytes()
        actual = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest().encode()
        if actual != oid and mode != b"120000":
            actual = git("hash-object", f"--path={path}", "--", path).strip()
        if actual != oid:
            raise AssertionError(f"Git blob mismatch: {path}")
        entries.append(path)
    with tarfile.open(destination, "w", format=tarfile.PAX_FORMAT, dereference=False) as archive:
        for path in entries:
            archive.add(root / path, arcname=path, recursive=False)
    with tarfile.open(destination) as archive:
        for entry in archive:
            disk = root / entry.name
            if entry.issym():
                assert entry.linkname == os.readlink(disk), f"Snapshot symlink mismatch: {entry.name}"
            else:
                assert entry.isfile() and archive.extractfile(entry).read() == disk.read_bytes(), f"Snapshot mismatch: {entry.name}"
    return len(entries)
