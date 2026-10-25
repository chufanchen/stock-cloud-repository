import io
import json
import tarfile

import pytest
from botocore.exceptions import ClientError

from ashare_cloud.common import write_json
from ashare_cloud.storage import Store, safe_extract


class FakeS3:
    def __init__(self):
        self.files = {}
        self.generation = 0

    def missing(self):
        raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

    def get_object(self, Bucket, Key):
        if Key not in self.files:
            self.missing()
        return {"Body": io.BytesIO(self.files[Key]), "ETag": str(self.generation)}

    def head_object(self, Bucket, Key):
        if Key not in self.files:
            self.missing()
        return {}

    def upload_file(self, Filename, Bucket, Key):
        from pathlib import Path
        self.files[Key] = Path(Filename).read_bytes()

    def download_file(self, Bucket, Key, Filename):
        from pathlib import Path
        Path(Filename).write_bytes(self.files[Key])

    def put_object(self, Bucket, Key, Body, ContentType, IfMatch=None, IfNoneMatch=None):
        if (IfNoneMatch and Key in self.files) or (IfMatch is not None and IfMatch != str(self.generation)):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.files[Key] = Body
        self.generation += 1
        return {"ETag": str(self.generation)}


def test_round_trip_and_concurrent_commit_rejected(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "market.db").write_bytes(b"database fixture")
    write_json(root / "account.json", {"last_settled": "2026-10-19"})
    client = FakeS3()
    first = Store(client, "bucket", "test")
    first.push(root, bootstrap=True)
    stale = Store(client, "bucket", "test")
    stale.pull(tmp_path / "restored")
    assert (tmp_path / "restored/market.db").read_bytes() == b"database fixture"
    write_json(root / "account.json", {"last_settled": "2026-10-20"})
    first.push(root)
    with pytest.raises(RuntimeError, match="其他任务"):
        stale.push(tmp_path / "restored")
    assert len([k for k in client.files if "/databases/" in k]) == 1
    with pytest.raises(RuntimeError):
        first.push(root, bootstrap=True)


@pytest.mark.parametrize("name,kind", [("../escape.json", "file"), ("/tmp/escape.json", "file"),
                                     ("link.json", "symlink")])
def test_unsafe_archives_rejected(tmp_path, name, kind):
    file = tmp_path / "bad.tar.gz"
    with tarfile.open(file, "w:gz") as t:
        m = tarfile.TarInfo(name)
        if kind == "symlink":
            m.type = tarfile.SYMTYPE
            m.linkname = "/tmp/escape"
        else:
            m.size = 1
        t.addfile(m, io.BytesIO(b"x") if kind == "file" else None)
    with pytest.raises(ValueError):
        safe_extract(file, tmp_path / "out")
