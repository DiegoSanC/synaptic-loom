"""Remote normalization table (DESIGN.md §3 / PLAN.md §T3).

The client and the server share EXACTLY this table.
"""
import pytest

from server.normalize import normalize

CASES = [
    ("git@github.com:Org/Repo.git", "github.com/org/repo"),
    ("https://github.com/Org/Repo.git", "github.com/org/repo"),
    ("https://user:pass@gitlab.com/a/b/", "gitlab.com/a/b"),
    ("ssh://git@bitbucket.org:7999/x/y.git", "bitbucket.org/x/y"),
    ("git@github.com:Org/Repo", "github.com/org/repo"),
]


@pytest.mark.parametrize("raw,expected", CASES)
def test_normalize_table(raw, expected):
    assert normalize(raw) == expected


def test_normalize_is_idempotent():
    for _, expected in CASES:
        assert normalize(expected) == expected


def test_normalize_strips_surrounding_whitespace():
    assert normalize("  git@github.com:Org/Repo.git\n") == "github.com/org/repo"


def test_normalize_empty_raises():
    with pytest.raises(ValueError):
        normalize("   ")
