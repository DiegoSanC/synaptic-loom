#!/usr/bin/env bash
# gitleaks_download_check.sh — the NETWORK leg of install.sh step 1b (D16), run by hand.
#
# The default suite never touches the network (client/tests/integration/
# test_install_gitleaks.py covers the step with a `curl` shim and a re-pinned copy of the
# installer). This script is the one place where the REAL install.sh downloads the REAL
# release asset from GitHub and verifies it against the sha256 values pinned in it. Run it
# after changing the pinned version or checksums, and before a release:
#
#   bash client/tests/manual/gitleaks_download_check.sh
#
# It works in a throwaway HOME/LOOM_HOME and touches nothing else (no venv, no trigger, no
# CLAUDE.md). Exit 0 = the official asset for this host downloaded, verified and runs.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/loom-gl-check.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/home"
HOME="$WORK/home" LOOM_HOME="$WORK/home/.loom" LOOM_SKIP_VENV=1 LOOM_SKIP_TRIGGER=1 \
  LOOM_SKIP_CLAUDE_MD=1 bash "$REPO_DIR/client/install.sh" --yes >"$WORK/out.txt" 2>&1 || {
  cat "$WORK/out.txt"
  echo "FAIL: install.sh exited non-zero" >&2
  exit 1
}
grep -q "sha256 verified" "$WORK/out.txt" || { cat "$WORK/out.txt"; echo "FAIL: no verification line" >&2; exit 1; }
version="$("$WORK/home/.loom/bin/gitleaks" version)"
if [ "$version" != "8.30.1" ]; then
  echo "FAIL: installed gitleaks reports '$version', expected 8.30.1" >&2
  exit 1
fi
grep -E "sha256 verified|installed at" "$WORK/out.txt"
echo "OK: the official gitleaks 8.30.1 asset downloaded, matched the pinned sha256 and runs."
