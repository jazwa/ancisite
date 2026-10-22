"""Per-db desired state: dirs, extensions, db-file presence.

Schema (migrations) is deliberately **not** here — `migrate` owns it. This
only makes the paths the app is handed (`ANCI_DB_*`) exist and valid.

Extensions ship to the content-addressed blob store and are symlinked into the
db's ext dir (so an unchanged or shared one isn't re-uploaded); one dropped from
the conf is pruned. `migrate` ships them the same way (it may `.load` one), via
`prune_script` + `run.upload_blob`.
"""

import shlex

from .. import errors, layout
from ..apply import LinkArtifact, sha256_file
from ..conf import conf_path
from .context import BlobUpload, DesiredState, ConvergeContext


def prune_script(ext_dir: str, keep: list[str]) -> str:
    """Drop any entry in `ext_dir` that isn't a currently-listed extension (so a
    removed one doesn't linger on the box, and thus on the app's EXT_DIR).
    Shared with `migrate`, which ships extensions the same way."""
    guard = "".join(f" ! -name {shlex.quote(n)}" for n in keep)
    return (f"[ -d {shlex.quote(ext_dir)} ] && "
            f"find {shlex.quote(ext_dir)} -mindepth 1 -maxdepth 1{guard} "
            f"-exec rm -rf {{}} + || true")


def contribute(ctx: ConvergeContext) -> DesiredState:
    c = DesiredState(name="dbs")
    for db_name in sorted(ctx.conf.dbs):
        db = ctx.conf.dbs[db_name]
        ext_dir = layout.ext_dir(db_name)
        c.prepare.append(
            f"install -d -o {layout.ANCI_USER} -g {layout.ANCI_GROUP} -m 0750 {ext_dir}\n"
            f"install -d -o {layout.ANCI_USER} -g {layout.ANCI_USER} -m 0750 "
            f"{layout.db_dir(db_name)}\n"
            # before (re)linking, prune extensions no longer in the conf
            + prune_script(ext_dir, [conf_path(ctx.conf, e).name for e in db.extensions])
        )
        # each extension is a blob in the store, symlinked into the db's ext dir
        # (so unchanged/shared files aren't re-uploaded; the app loads by name)
        for ext in db.extensions:
            p = conf_path(ctx.conf, ext)
            if not p.is_file():
                raise errors.AppError(
                    f"{ext} doesn't exist",
                    why=f"that's an extension listed under [db.{db_name}]",
                    try_="build/obtain it, or remove it from 'extensions'",
                )
            sha = sha256_file(p)
            c.store_blobs.append(BlobUpload(exe_name=db_name, local_path=p,
                                            dest=layout.blob_path(sha), sha=sha))
            c.links.append(LinkArtifact(path=f"{ext_dir}/{p.name}",
                                        target=layout.blob_path(sha)))
    return c
