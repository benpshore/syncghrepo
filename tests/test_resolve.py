import pytest

from syncghrepo import resolve


@pytest.mark.parametrize(
    ("path", "upstream", "mine", "how", "content"),
    [
        ("src/a.py", b"x = 1\n", b"x = 1\n", "identical", b"x = 1\n"),
        ("src/a.py", b"x = 1  \ny\n", b"x = 1\r\ny\n\n\n", "whitespace", b"x = 1\r\ny\n\n\n"),
        ("uv.lock", b"up\n", b"mine\n", "lockfile", b"up\n"),
        ("sub/Cargo.lock", b"up\n", b"mine\n", "lockfile", b"up\n"),
        ("go.sum", b"b 1\na 1\n", b"a 1\nc 1\n", "union", b"a 1\nb 1\nc 1\n"),
    ],
)
def test_classify_trivial(path, upstream, mine, how, content):
    choice = resolve.classify(path, upstream, mine)
    assert choice["how"] == how
    assert choice["content"] == content


@pytest.mark.parametrize(
    ("upstream", "mine"),
    [
        (b"x = 1\n", b"x = 2\n"),  # real change
        (b"if a:\n    b()\n", b"if a:\n  b()\n"),  # indentation is meaningful
        (b"a b\n", b"ab\n"),  # whitespace inside a line is meaningful
        (None, b"x\n"),  # deleted upstream, changed here
        (b"x\n", None),
    ],
)
def test_classify_needs_a_person(upstream, mine):
    assert resolve.classify("src/a.py", upstream, mine) is None


def test_lockfile_choice_names_the_regenerate_command():
    assert resolve.classify("uv.lock", b"a", b"b")["regenerate"] == ["uv", "lock"]
    assert resolve.classify("package-lock.json", b"a", b"b")["regenerate"][:2] == ["npm", "install"]
