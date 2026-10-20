"""syncghrepo: safely sync local clones of Ben's GitHub repos and report open work."""

from .cli import main

__all__ = ["hello", "main"]


def hello() -> str:
    return "Hello from syncghrepo!"
