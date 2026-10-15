"""D16 part 4 — install.sh installs gitleaks 8.30.1, checksum-verified, into $LOOM_HOME/bin.

What is asserted:
- the sha256 values install.sh accepts are the OFFICIAL ones (literals below, copied from
  ``gitleaks_8.30.1_checksums.txt`` of the v8.30.1 release, fetched 2026-10-15 — an
  independent source, not derived from install.sh);
- a tarball whose sha256 does not match is refused loudly and NOTHING is installed — and
  no environment variable or flag can make it accepted;
- an unsupported platform aborts before anything is downloaded;
- an installed binary that already reports 8.30.1 is not downloaded again; an older one
  is upgraded, and a refused upgrade leaves the old one in place;
- the download goes to the official release URL (a ``curl`` shim records it: no network);
- an existing ``config.json`` ends up with ``scanner = gitleaks / 8.30.1``;
- ``--update`` runs the step too.

The happy path needs a tarball whose sha256 is pinned. The real release asset is 8 MB and
is not vendored, so the happy-path tests run a COPY of install.sh whose pinned value for
THIS host's platform is swapped for the sha256 of a synthetic tarball built here. The
copy is a test artefact; the real script's table is pinned by the first test. The real
network download is NOT in this suite (no network in tests): it is exercised by hand with
``client/tests/manual/gitleaks_download_check.sh``.

Hard rules (as in ``test_install_update.py``): HOME/LOOM_HOME under tmp, LOOM_SKIP_VENV=1,
LOOM_SKIP_TRIGGER=1, and a PATH sandbox with no scheduler and no real ``curl``.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_SCRIPT = REPO_ROOT / "client" / "install.sh"
VERSION = "8.30.1"

# From https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_checksums.txt
OFFICIAL_SHA256 = {
    "darwin_arm64": "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5",
    "darwin_x64": "dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709",
    "linux_arm64": "e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080",
    "linux_x64": "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
}
OFFICIAL_URL = ("https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/"
                "gitleaks_8.30.1_{platform}.tar.gz")

SAFE_TOOLS = (
    "python3", "git", "jq", "bash", "sh", "env", "cat", "mv", "cp", "mkdir", "rm",
    "date", "diff", "grep", "sed", "chmod", "uname", "dirname", "basename", "head",
    "tail", "tr", "wc", "sort", "touch", "ln", "sleep", "mktemp", "readlink", "id",
    "awk", "cut", "find", "tar", "gzip", "shasum", "sha256sum",
)
CHECKOUT_FILES = (
    "client/install.sh", "client/hooks/sessionend.sh", "client/skills/loom/SKILL.md",
    "client/skills/loom-register/SKILL.md", "client/skills/loom-feedback/SKILL.md",
    "client/timer/loom-process.service", "client/timer/loom-process.timer",
    "prompts/summarizer_v1.md", "prompts/summarizer_v2.md", "prompts/summarizer_v3.md",
    "prompts/summarizer_v4.md", "prompts/digest_v1.md",
)
GIT_ENV = {"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@x",
           "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@x"}


def _host_platform() -> str:
    os_name = {"Darwin": "darwin", "Linux": "linux"}[platform.system()]
    arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64",
            "aarch64": "arm64"}[platform.machine().lower()]
    return f"{os_name}_{arch}"


# --------------------------------------------------------------------------- #
# Sandbox                                                                      #
# --------------------------------------------------------------------------- #
def _sandbox(tmp_path: Path) -> Path:
    shims = tmp_path / "shims"
    shims.mkdir(exist_ok=True)
    safe = tmp_path / "safe_bin"
    safe.mkdir(exist_ok=True)
    for name in SAFE_TOOLS:
        target = shutil.which(name)
        if target and not (safe / name).exists():
            (safe / name).symlink_to(target)
    assert shutil.which("curl", path=str(safe)) is None
    assert shutil.which("crontab", path=str(safe)) is None
    return shims


def _curl_shim(tmp_path: Path, *, serve: Path | None = None) -> Path:
    """Fake ``curl``: records argv; writes ``serve`` to the ``-o`` path (or fails)."""
    record = tmp_path / "curl_calls.txt"
    shim = _sandbox(tmp_path) / "curl"
    shim.write_text(
        "#!/usr/bin/env python3\nimport pathlib, shutil, sys\n"
        f"pathlib.Path({str(record)!r}).open('a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"serve = {str(serve) if serve else None!r}\n"
        "if serve is None:\n    sys.exit(22)\n"
        "out = sys.argv[sys.argv.index('-o') + 1]\n"
        "shutil.copyfile(serve, out)\n",
        encoding="utf-8")
    shim.chmod(0o755)
    return record


def _uname_shim(tmp_path: Path, system: str, machine: str) -> None:
    shim = _sandbox(tmp_path) / "uname"
    shim.write_text(
        "#!/bin/sh\ncase \"$1\" in\n"
        f"  -s) echo {system} ;;\n  -m) echo {machine} ;;\n  *) echo {system} ;;\nesac\n",
        encoding="utf-8")
    shim.chmod(0o755)


def _tarball(path: Path, *, version: str = VERSION, marker: str = "synthetic") -> Path:
    """A release-shaped tarball (gitleaks + LICENSE + README) with a fake binary."""
    script = (f"#!/bin/sh\n# {marker}\nif [ \"$1\" = version ]; then echo {version}; "
              "exit 0; fi\ncat >/dev/null; echo '[]'\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:
        for name, data, mode in (("LICENSE", b"MIT\n", 0o644), ("README.md", b"x\n", 0o644),
                                 ("gitleaks", script, 0o755)):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            tar.addfile(info, io.BytesIO(data))
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repinned_copy(tmp_path: Path, tarball: Path) -> Path:
    """A mini checkout whose install.sh accepts ``tarball`` for THIS host's platform."""
    root = tmp_path / "checkout"
    for rel in CHECKOUT_FILES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / rel, root / rel)
    script = root / "client" / "install.sh"
    text = script.read_text(encoding="utf-8")
    official = OFFICIAL_SHA256[_host_platform()]
    assert text.count(official) == 1
    script.write_text(text.replace(official, _sha256(tarball)), encoding="utf-8")
    return script


def _run(tmp_path: Path, *args: str, script: Path = INSTALL_SCRIPT,
         **extra_env: str) -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    shims = _sandbox(tmp_path)
    env = {
        "HOME": str(home), "LOOM_HOME": str(home / ".loom"), "LOOM_SKIP_VENV": "1",
        "LOOM_SKIP_TRIGGER": "1", "LOOM_SKIP_CLAUDE_MD": "1",
        "PATH": f"{shims}{os.pathsep}{tmp_path / 'safe_bin'}",
        "TMPDIR": str(tmp_path), **GIT_ENV, **extra_env,
    }
    return subprocess.run(["bash", str(script), "--yes", *args], env=env,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          timeout=180)


def _installed(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".loom" / "bin" / "gitleaks"


def _seed_binary(tmp_path: Path, version: str) -> Path:
    path = _installed(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n# seeded\n[ \"$1\" = version ] && echo {version}\n",
                    encoding="utf-8")
    path.chmod(0o755)
    return path


# --------------------------------------------------------------------------- #
# The pinned table                                                             #
# --------------------------------------------------------------------------- #
def test_install_sh_pins_exactly_the_official_checksums():
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert f'GITLEAKS_VERSION="{VERSION}"' in text
    for plat, sha in OFFICIAL_SHA256.items():
        assert f'{plat}) echo "{sha}"' in text, plat
    assert "releases/download/v$GITLEAKS_VERSION" in text
    assert "gitleaks_8.30.1_checksums.txt" in text  # the source is cited


# --------------------------------------------------------------------------- #
# Happy path (re-pinned copy)                                                  #
# --------------------------------------------------------------------------- #
def test_a_verified_tarball_is_installed_into_loom_home_bin(tmp_path):
    tarball = _tarball(tmp_path / "mirror" / "gl.tar.gz")
    script = _repinned_copy(tmp_path, tarball)
    calls = _curl_shim(tmp_path)

    proc = _run(tmp_path, script=script, LOOM_GITLEAKS_TARBALL=str(tarball))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    binary = _installed(tmp_path)
    assert binary.is_file() and not binary.is_symlink()
    assert stat.S_IMODE(binary.stat().st_mode) & 0o111
    assert subprocess.run([str(binary), "version"], capture_output=True,
                          text=True).stdout.strip() == VERSION
    assert "synthetic" in binary.read_text()
    assert not calls.exists(), "a local tarball must not trigger a download"
    assert "sha256 verified" in proc.stdout
    assert not list(tmp_path.glob("loom-gitleaks.*")), "the temp dir is cleaned up"


def test_the_download_goes_to_the_official_release_url(tmp_path):
    """No tarball given → ``curl`` fetches the official asset for this platform (shim)."""
    tarball = _tarball(tmp_path / "served.tar.gz")
    script = _repinned_copy(tmp_path, tarball)
    calls = _curl_shim(tmp_path, serve=tarball)

    proc = _run(tmp_path, script=script)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    argv = calls.read_text().splitlines()
    assert len(argv) == 1
    assert argv[0].endswith(OFFICIAL_URL.format(platform=_host_platform()))
    assert "--proto =https" in argv[0]
    assert _installed(tmp_path).is_file()


def test_an_older_installed_version_is_upgraded(tmp_path):
    tarball = _tarball(tmp_path / "gl.tar.gz")
    script = _repinned_copy(tmp_path, tarball)
    _seed_binary(tmp_path, "8.18.0")

    proc = _run(tmp_path, script=script, LOOM_GITLEAKS_TARBALL=str(tarball))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "synthetic" in _installed(tmp_path).read_text()


def test_an_existing_config_is_set_to_mandatory_gitleaks(tmp_path):
    tarball = _tarball(tmp_path / "gl.tar.gz")
    script = _repinned_copy(tmp_path, tarball)
    config = tmp_path / "home" / ".loom" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"server_url": "http://x", "token": "t",
                                  "scanner": {"engine": "auto", "gitleaks_version": None},
                                  "capture_mode": "opt_in"}), encoding="utf-8")
    config.chmod(0o600)

    proc = _run(tmp_path, script=script, LOOM_GITLEAKS_TARBALL=str(tarball))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["scanner"] == {"engine": "gitleaks", "gitleaks_version": VERSION}
    assert data["token"] == "t" and data["capture_mode"] == "opt_in"
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


# --------------------------------------------------------------------------- #
# Refusals (the REAL install.sh)                                               #
# --------------------------------------------------------------------------- #
def _assert_refused(proc, tmp_path):
    assert proc.returncode != 0, proc.stdout
    assert "CHECKSUM MISMATCH" in proc.stderr
    assert "Nothing was installed" in proc.stderr
    assert not (tmp_path / "home" / ".claude" / "settings.json").exists(), \
        "the install must stop before step 4"


def test_a_tarball_with_the_wrong_checksum_is_refused_and_nothing_installed(tmp_path):
    tarball = _tarball(tmp_path / "evil.tar.gz", marker="evil")

    proc = _run(tmp_path, LOOM_GITLEAKS_TARBALL=str(tarball))

    _assert_refused(proc, tmp_path)
    assert not _installed(tmp_path).exists()
    assert OFFICIAL_SHA256[_host_platform()] in proc.stderr   # expected value shown
    assert _sha256(tarball) in proc.stderr                     # and what was received


def test_a_bad_download_is_refused_too(tmp_path):
    served = _tarball(tmp_path / "served.tar.gz", marker="evil")
    _curl_shim(tmp_path, serve=served)

    proc = _run(tmp_path)

    _assert_refused(proc, tmp_path)
    assert not _installed(tmp_path).exists()


@pytest.mark.parametrize("bypass", [
    {"LOOM_SKIP_CHECKSUM": "1"}, {"LOOM_GITLEAKS_SHA256": "SELF"},
    {"GITLEAKS_SHA256": "SELF"}, {"LOOM_INSECURE": "1"}, {"LOOM_GITLEAKS_VERIFY": "0"},
    {"GITLEAKS_VERSION": "8.18.0"}, {"LOOM_GITLEAKS_PINNED_SHA256": "SELF"},
])
def test_no_environment_variable_bypasses_the_checksum(tmp_path, bypass):
    tarball = _tarball(tmp_path / "evil.tar.gz", marker="evil")
    env = {k: (_sha256(tarball) if v == "SELF" else v) for k, v in bypass.items()}

    proc = _run(tmp_path, LOOM_GITLEAKS_TARBALL=str(tarball), **env)

    _assert_refused(proc, tmp_path)
    assert not _installed(tmp_path).exists()


@pytest.mark.parametrize("flag", ["--insecure", "--skip-checksum", "--no-verify"])
def test_no_flag_bypasses_the_checksum(tmp_path, flag):
    tarball = _tarball(tmp_path / "evil.tar.gz", marker="evil")
    proc = _run(tmp_path, flag, LOOM_GITLEAKS_TARBALL=str(tarball))
    assert proc.returncode == 2 and "unknown argument" in proc.stderr
    assert not _installed(tmp_path).exists()


def test_a_refused_upgrade_leaves_the_old_binary_alone(tmp_path):
    seeded = _seed_binary(tmp_path, "8.18.0")
    before = seeded.read_bytes()
    tarball = _tarball(tmp_path / "evil.tar.gz", marker="evil")

    proc = _run(tmp_path, LOOM_GITLEAKS_TARBALL=str(tarball))

    _assert_refused(proc, tmp_path)
    assert seeded.read_bytes() == before


@pytest.mark.parametrize("system, machine", [("FreeBSD", "amd64"), ("Linux", "riscv64"),
                                             ("Darwin", "i386")])
def test_an_unsupported_platform_aborts_before_downloading(tmp_path, system, machine):
    _sandbox(tmp_path)
    _uname_shim(tmp_path, system, machine)
    calls = _curl_shim(tmp_path)

    proc = _run(tmp_path)

    assert proc.returncode != 0
    assert "unsupported platform" in proc.stderr
    assert not calls.exists()
    assert not _installed(tmp_path).exists()


def test_an_installed_8_30_1_is_not_downloaded_again(tmp_path):
    seeded = _seed_binary(tmp_path, VERSION)
    before = seeded.read_bytes()
    calls = _curl_shim(tmp_path)

    proc = _run(tmp_path)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not calls.exists()
    assert seeded.read_bytes() == before
    assert "already installed" in proc.stdout


def test_the_skip_seam_installs_nothing_and_says_capture_stays_paused(tmp_path):
    calls = _curl_shim(tmp_path)
    proc = _run(tmp_path, LOOM_SKIP_GITLEAKS="1")
    assert proc.returncode == 0, proc.stderr
    assert not calls.exists() and not _installed(tmp_path).exists()
    assert "PAUSED" in proc.stdout


# --------------------------------------------------------------------------- #
# --update                                                                     #
# --------------------------------------------------------------------------- #
def test_update_installs_gitleaks_too(tmp_path):
    src = tmp_path / "origin_work"
    for rel in CHECKOUT_FILES:
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / rel, src / rel)
    env = {**os.environ, **GIT_ENV}
    subprocess.run(["git", "init", "-q"], cwd=src, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=src, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "mini"], cwd=src, check=True, env=env)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(src), str(work)], check=True, env=env)
    tarball = _tarball(tmp_path / "evil.tar.gz", marker="evil")

    proc = _run(tmp_path, "--update", script=work / "client" / "install.sh",
                LOOM_GITLEAKS_TARBALL=str(tarball))

    _assert_refused(proc, tmp_path)   # the step ran under --update, and refused
    assert "0/5" in proc.stdout
