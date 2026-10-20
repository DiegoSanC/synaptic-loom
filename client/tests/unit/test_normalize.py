"""Tests for normalize() — normative table from DESIGN.md §3.

The server shares this very table (DESIGN.md §3): if it changes here, it changes there.
"""
import pytest

from loom.normalize import normalize

# (raw input, expected canonical output)
CASES = [
    # Minimal normative table (DESIGN.md §3).
    ("git@github.com:Org/Repo.git", "github.com/org/repo"),
    ("https://github.com/Org/Repo.git", "github.com/org/repo"),
    ("https://user:pass@gitlab.com/a/b/", "gitlab.com/a/b"),
    ("ssh://git@bitbucket.org:7999/x/y.git", "bitbucket.org/x/y"),  # port dropped
    ("git@github.com:Org/Repo", "github.com/org/repo"),            # no .git
    # Reasonable extras.
    ("git://github.com/Org/Repo.git", "github.com/org/repo"),      # git:// protocol
    ("  git@github.com:Org/Repo.git  ", "github.com/org/repo"),    # whitespace
    ("http://github.com/Org/Repo", "github.com/org/repo"),         # plain http
    ("https://GitHub.com/Org/Repo.git", "github.com/org/repo"),    # uppercase host
    ("github.com:Org/Repo.git", "github.com/org/repo"),            # scp without user@
    ("ssh://git@github.com/Org/Repo.git", "github.com/org/repo"),  # ssh without a port
    ("https://token@github.com/Org/Repo.git", "github.com/org/repo"),  # user only, no pass
    ("git@github.com:Org/Repo.git/", "github.com/org/repo"),       # trailing slash
]


@pytest.mark.parametrize("raw,expected", CASES)
def test_normalize_table(raw, expected):
    assert normalize(raw) == expected


@pytest.mark.parametrize("bad", ["", "   ", "\n\t", "garbage-no-host", "///", "https://", "git@host:"])
def test_normalize_rejects_garbage(bad):
    with pytest.raises(ValueError):
        normalize(bad)


def test_normalize_rejects_non_str():
    with pytest.raises(ValueError):
        normalize(None)  # type: ignore[arg-type]
