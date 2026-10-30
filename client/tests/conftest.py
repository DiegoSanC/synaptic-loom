"""Guards every client test (unit, integration, e2e) shares.

``CLAUDE_CONFIG_DIR`` is dropped before any test runs. install.sh, doctor and enrolment
read it to find Claude Code's configuration directory, and the tests point ``HOME`` at a
tmpdir to keep the real ``~/.claude`` out of reach: a variable inherited from the shell
(a ``CLAUDE_CONFIG_DIR=... claude`` wrapper sets it for every command its session runs)
would aim them at the real directory instead. A test that needs it sets it itself.
"""
import os

os.environ.pop("CLAUDE_CONFIG_DIR", None)
