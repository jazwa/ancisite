from anci import forward

# the real one, for the tests of the forward itself (which fake its sudo and files)
REAL_RECONCILE = forward.reconcile


def _no_forward(conf, env_name):
    raise AssertionError("a test reached the localhost forward: stub anci.forward.reconcile")


# a test must never install the real forward (it writes nftables rules on this
# machine with sudo); tests that converge stub `reconcile` themselves, which
# overrides this guard
forward.reconcile = _no_forward


from anci.inspect import vendored  # noqa: E402

# the real one, for the vendored resource's own tests
REAL_BINARIES = vendored.binaries

# no test downloads (or hashes) the real caddy/litestream/sqlite3: by default an
# env pins none; a test that wants some patches `binaries` itself
vendored.binaries = lambda conf, env_name: {}
