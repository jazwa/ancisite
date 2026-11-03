"""litestream.yaml: every replicated db on the box, s3-compatible replicas.

Emitted as JSON (valid YAML). Credentials are `${VAR}` references that
litestream expands from its environment (the secrets file) at load.
"""

from __future__ import annotations

import json

from .. import layout


def replica_target(db) -> str:
    r = db.replication
    assert r is not None
    return f"{r.bucket}/{r.path or db.name}"


def render_litestream_yaml(dbs: list) -> str:
    def replica(db) -> dict:
        r = db.replication
        out = {"type": "s3", "bucket": r.bucket, "path": r.path or db.name}
        if r.endpoint:
            out["endpoint"] = r.endpoint
        if r.region:
            out["region"] = r.region
        if r.access_key_env and r.secret_key_env:
            out["access-key-id"] = f"${{{r.access_key_env}}}"
            out["secret-access-key"] = f"${{{r.secret_key_env}}}"
        if r.sync_interval:
            out["sync-interval"] = r.sync_interval
        if r.retention:
            out["retention"] = r.retention
        return out

    doc = {"socket": {"enabled": True, "path": layout.LITESTREAM_SOCKET},
           "dbs": [{"path": layout.db_path(db.name), "replicas": [replica(db)]}
                   for db in dbs]}
    return json.dumps(doc, indent=2) + "\n"
