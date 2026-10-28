"""user.<name>: a person's access to the box — `anci` group membership, sudoers
grant, and `authorized_keys`."""

from __future__ import annotations

from ..layout import ANCI_GROUP, split_safe
from ..render.shell import authorized_keys, grant_script, revoke_script, sudoers_line
from .core import Resource, Step, pairs, sha

_SUDOERS = "/etc/sudoers.d"
_HOME = "/home"   # where the accounts script puts each user's home


class User(Resource):
    name = "user"
    fields = frozenset({"group", "sudoers", "keys"})
    rows = {"CREATE": "grant", "group": "grant", "sudoers": "grant", "keys": "grant",
            "DELETE": "revoke"}

    def target(self, conf, env_name):
        return {
            f"user.{n}": {"group": True,
                          "sudoers": sha(sudoers_line(n)),
                          "keys": sha(authorized_keys(keys))}
            for n, keys in conf.users_for_env(env_name).items()
        }

    def probes(self):
        group = f"getent group {ANCI_GROUP} | cut -d: -f4 | tr ',' '\\n'"
        grants = f'cd {_SUDOERS} && for f in anci-*; do [ -f "$f" ] && echo "${{f#anci-}}"; done'
        return {
            "group": group,
            "sudoers": f'cd {_SUDOERS} && for f in anci-*; do [ -f "$f" ] && '
                       f'echo "${{f#anci-}} $(sha256sum "$f" | cut -c1-64)"; done',
            # every name anci may have given access: group members, plus sudoers
            # grants (revoking removes the membership but has kept the grant)
            "keys": f'for u in $({{ {group}; {grants}; }} | sort -u); do k="{_HOME}/$u/.ssh/authorized_keys"; '
                    f'[ -f "$k" ] && echo "$u $(sha256sum "$k" | cut -c1-64)"; done',
        }

    def observed(self, found):
        group = {u for u in found["group"] if u}
        sudoers, keys = pairs(found["sudoers"]), pairs(found["keys"])
        return {
            f"user.{n}": {"group": n in group, "sudoers": sudoers.get(n), "keys": keys.get(n)}
            for n in split_safe(sorted(group | sudoers.keys() | keys.keys()))[0]
        }

    def steps(self, change, ctx):
        name = change.identifier.split(".", 1)[1]
        if "revoke" in self.rows_for(change):
            return [Step(f"user.{name}/revoke", revoke_script(name))]
        keys = ctx.conf.users_for_env(ctx.env_name)[name]
        return [Step(f"user.{name}/grant", grant_script(name, keys))]
