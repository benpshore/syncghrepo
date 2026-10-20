# Test fixtures

`git-sync` is an unmodified copy of [simonthum/git-sync](https://github.com/simonthum/git-sync)
(CC0, public domain), the same file as the one installed at `~/.local/bin/git-sync`.
Tests run syncghrepo against it so the real auto-commit, rebase and push behaviour
is covered, including in CI. It is used by the tests only; syncghrepo itself finds
git-sync on the machine (`--git-sync`, `$SYNCGHREPO_GIT_SYNC`, `PATH`, `~/.local/bin`).
`tests/test_gitsync.py` checks that this copy matches the installed one when present.
