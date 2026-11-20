"""A CRLF export is not a byte-for-byte backup of an LF checkout."""
import subprocess
import tarfile

import pytest

from scripts.checkout_snapshot import backup_checkout


def test_snapshot_preserves_bytes_and_rejects_drift(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args])

    git("init", "-q")
    (root / ".gitattributes").write_text("*.ps1 text eol=crlf\n")
    script = root / "test.ps1"
    script.write_bytes(b"echo hello\n")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
    old_archive = tmp_path / "old.tar"
    git("archive", "--format=tar", "-o", str(old_archive), "HEAD")
    with tarfile.open(old_archive) as archive:
        with pytest.raises(AssertionError):
            assert archive.extractfile("test.ps1").read() == script.read_bytes()
    snapshot = tmp_path / "snapshot.tar"
    assert backup_checkout(root, "HEAD", snapshot) == 2
    with tarfile.open(snapshot) as archive:
        assert archive.extractfile("test.ps1").read() == b"echo hello\n"
    script.write_bytes(b"echo corrupted\n")
    with pytest.raises(AssertionError, match="Git blob mismatch"):
        backup_checkout(root, "HEAD", snapshot)
