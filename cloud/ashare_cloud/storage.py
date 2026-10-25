"""R2 content-addressed database + small immutable state bundles + atomic HEAD CAS."""
from __future__ import annotations

import gzip
import json
import os
import shutil
import tarfile
import tempfile
import uuid
from pathlib import Path

from .common import file_hash, read_json, write_json


def safe_extract(archive, destination):
    dest = Path(destination).resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for m in tar.getmembers():
            p = (dest / m.name).resolve()
            if not p.is_relative_to(dest) or not m.isfile() or m.size > 64 * 1024 * 1024:
                raise ValueError("R2 状态包含非法路径、链接或异常大文件")
        tar.extractall(dest, filter="data")


class Store:
    def __init__(self, client=None, bucket=None, prefix=None):
        self.bucket = bucket or os.environ["R2_BUCKET"]
        self.prefix = (prefix or os.getenv("R2_PREFIX") or "wuming-v2").strip("/")
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client("s3", region_name="auto", endpoint_url=os.environ["R2_ENDPOINT_URL"],
                                  aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                                  aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                                  config=Config(retries={"max_attempts": 4, "mode": "standard"},
                                                request_checksum_calculation="when_required",
                                                response_checksum_validation="when_required"))
        self.client = client
        self.etag = None
        self.manifest = None

    def key(self, s):
        return f"{self.prefix}/{s}"

    def head(self):
        from botocore.exceptions import ClientError
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self.key("HEAD.json"))
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise
        self.etag = obj["ETag"]
        self.manifest = json.loads(obj["Body"].read())
        return self.manifest

    def pull(self, root):
        root = Path(root)
        if not self.head():
            raise RuntimeError("R2 尚未初始化；先执行 bootstrap")
        m = self.manifest
        root.mkdir(parents=True, exist_ok=True)
        if any(root.iterdir()):
            raise ValueError("R2 恢复目录必须为空，以免覆盖本地资料")
        with tempfile.TemporaryDirectory() as tmp:
            archive, database = Path(tmp) / "state.tar.gz", Path(tmp) / "db.gz"
            self.client.download_file(self.bucket, m["state_key"], str(archive))
            if file_hash(archive) != m["state_sha256"]:
                raise ValueError("R2 状态包校验失败")
            safe_extract(archive, root)
            self.client.download_file(self.bucket, m["database_key"], str(database))
            with gzip.open(database, "rb") as src, (root / "market.db").open("wb") as dst:
                shutil.copyfileobj(src, dst)
        if file_hash(root / "market.db") != m["database_sha256"]:
            raise ValueError("R2 数据库校验失败")

    def push(self, root, bootstrap=False):
        from botocore.exceptions import ClientError
        root = Path(root)
        db_hash = file_hash(root / "market.db")
        db_key = self.key(f"databases/{db_hash}.sqlite.gz")
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "state.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                for p in sorted(root.rglob("*")):
                    if p.is_file() and p.suffix in (".json", ".md", ".html"):
                        tar.add(p, arcname=p.relative_to(root), recursive=False)
            key = self.key(f"states/{uuid.uuid4().hex}.tar.gz")
            try:
                self.client.head_object(Bucket=self.bucket, Key=db_key)
            except ClientError as e:
                if e.response["Error"]["Code"] not in ("NoSuchKey", "404", "NotFound"):
                    raise
                zipped = Path(tmp) / "database.gz"
                with (root / "market.db").open("rb") as src, gzip.open(zipped, "wb", compresslevel=3) as dst:
                    shutil.copyfileobj(src, dst)
                self.client.upload_file(str(zipped), self.bucket, db_key)
            self.client.upload_file(str(archive), self.bucket, key)
            manifest = {"version": 1, "state_key": key, "state_sha256": file_hash(archive),
                        "database_key": db_key, "database_sha256": db_hash,
                        "last_settled": read_json(root / "account.json").get("last_settled")}
            conditional = {"IfNoneMatch": "*"} if bootstrap else {"IfMatch": self.etag}
            if not bootstrap and not self.etag:
                raise ValueError("未读取远端版本，禁止覆盖状态")
            try:
                response = self.client.put_object(Bucket=self.bucket, Key=self.key("HEAD.json"),
                    Body=json.dumps(manifest).encode(), ContentType="application/json", **conditional)
            except ClientError as e:
                if e.response["Error"]["Code"] in ("PreconditionFailed", "412", "ConditionalRequestConflict"):
                    raise RuntimeError("R2 状态已被其他任务更新；本次结果未发布。重新从最新状态运行。") from None
                raise
            self.etag, self.manifest = response["ETag"], manifest
            return manifest
