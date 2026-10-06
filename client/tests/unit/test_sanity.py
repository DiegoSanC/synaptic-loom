"""Sanity check: the `loom` client package imports and exposes a callable main()."""
import loom
from loom.cli import main


def test_package_imports():
    assert loom.__name__ == "loom"
    assert callable(main)
