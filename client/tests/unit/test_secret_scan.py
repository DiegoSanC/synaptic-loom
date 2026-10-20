"""Tests for secret_scan — each regex family with a positive and a negative (DESIGN.md §1, P3).

The critical negatives (40-hex git SHA, UUID) must not trigger ANY family: they are
the false alarms that would make the secret gate useless.
"""
import pytest

from loom import secret_scan


def _kinds(text: str) -> set[str]:
    return {f.kind for f in secret_scan.scan(text)}


# --- Positives: one representative input per family ------------------------------

@pytest.mark.parametrize(
    "kind, sample",
    [
        ("aws_access_key", "clave AKIAIOSFODNN7EXAMPLE en el log"),
        ("openai_key", "usa sk-test1234567890abcdef como key"),
        ("github_pat", "token ghp_" + "a" * 36),
        (
            "jwt",
            "Authorization: Bearer "
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
        ),
        ("private_key", "-----BEGIN RSA PRIVATE KEY-----\nMIIE..."),
        ("private_key", "-----BEGIN OPENSSH PRIVATE KEY-----"),
        ("private_key", "-----BEGIN PRIVATE KEY-----"),
        ("password_assignment", "config: password=hunter2 guardado"),
        ("password_assignment", "PASSWD: s3cr3t-value"),
        ("url_credentials", "DB en postgres://user:secretpass@db.host:5432/app"),
        ("url_credentials", "cache redis://admin:pw123@10.0.0.1:6379/0"),
        ("email", "escribe a diego@example.test para dudas"),
    ],
)
def test_family_positive(kind, sample):
    assert kind in _kinds(sample), f"{kind!r} should be detected in {sample!r}"


# --- Negatives: one clean input per family ---------------------------------------

@pytest.mark.parametrize(
    "sample",
    [
        "AKIA es un prefijo pero AKIA-short no es una clave",   # AWS: invalid length/shape
        "sk-short",                                              # OpenAI: <16 chars after sk-
        "ghp_tooshort",                                          # GitHub: <36 chars
        "eyJ solo, sin los tres segmentos del token",           # JWT: no 3 segments
        "-----BEGIN CERTIFICATE-----",                          # PEM: not a PRIVATE KEY
        "the user must enter a password to continue",           # password with no '=value'
        "clona desde https://github.com/org/repo",              # URL without user:pass@
        "el commit y la rama, sin datos personales",            # no email
    ],
)
def test_family_negative(sample):
    assert secret_scan.scan(sample) == []


# --- Critical negatives: identifiers that are NOT secrets ------------------------

def test_git_sha_40_hex_is_not_a_secret():
    # 40-hex git SHA: no family must fire.
    assert secret_scan.scan("commit a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0 revisado") == []


def test_plain_uuid_is_not_a_secret():
    assert secret_scan.scan("session_id 550e8400-e29b-41d4-a716-446655440000") == []


def test_empty_and_none_safe():
    assert secret_scan.scan("") == []
    assert secret_scan.scan("texto normal sin nada") == []


def test_finding_redaction_hides_the_value():
    findings = secret_scan.scan("sk-test1234567890abcdef")
    assert findings
    red = findings[0].redacted
    assert red.startswith("sk-t")
    assert "1234567890" not in red      # the body of the secret stays masked
    assert set(red[4:]) == {"*"}
