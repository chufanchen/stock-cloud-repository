"""Small allowlisted presentation bundle; no account, DB, prompts or secrets."""
from __future__ import annotations
import hashlib
import json
import os
import uuid
from .common import now
from .storage import Store

MAX_BYTES = 32 * 1024 * 1024


def public_store(store=None):
    base = store or Store()
    return Store(base.client, os.getenv('R2_PUBLIC_BUCKET') or base.bucket, base.prefix)


def publish(root, store=None):
    from .pages import load_public_data
    store = public_store(store)
    data = load_public_data(root)
    body = json.dumps({'schema': 1, 'generated_at': now().isoformat(), 'data': data},
                      ensure_ascii=False, allow_nan=False).encode()
    if len(body) > MAX_BYTES:
        raise ValueError('网页结果包超过32MiB，请缩短展示归档范围')
    key = store.key('publications/versions/' + uuid.uuid4().hex + '.json')
    store.client.put_object(Bucket=store.bucket, Key=key, Body=body, ContentType='application/json', IfNoneMatch='*')
    pointer = {'schema': 1, 'key': key, 'sha256': hashlib.sha256(body).hexdigest(), 'generated_at': now().isoformat()}
    store.client.put_object(Bucket=store.bucket, Key=store.key('publications/HEAD.json'),
                            Body=json.dumps(pointer).encode(), ContentType='application/json')
    return pointer


def fetch(store=None):
    store = public_store(store)
    head = json.loads(store.client.get_object(Bucket=store.bucket, Key=store.key('publications/HEAD.json'))['Body'].read(8192))
    if not head['key'].startswith(store.key('publications/versions/')):
        raise ValueError('网页结果包位置错误')
    body = store.client.get_object(Bucket=store.bucket, Key=head['key'])['Body'].read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES or hashlib.sha256(body).hexdigest() != head['sha256']:
        raise ValueError('网页结果包校验失败')
    bundle = json.loads(body)
    if bundle.get('schema') != 1 or not isinstance(bundle.get('data'), dict):
        raise ValueError('网页结果包格式不兼容')
    return bundle
