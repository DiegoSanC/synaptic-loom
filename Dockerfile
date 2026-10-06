# Synaptic Loom pilot API — the image Fly.io runs (issue #2).
#
# Both stages pin the SAME digest, deliberately. A floating `:3.12-slim` tag would make
# AC1's "builds reproducibly" untrue on the day Debian rebuilds the image, and a
# builder/runtime digest mismatch would mean the virtualenv was linked against a different
# libpython than the one that ends up importing it.
#
# python:3.12-slim is Debian bookworm, whose libsqlite3 ships ENABLE_FTS5. That is not a
# nicety: `server/search.py:_require_fts5` ABORTS startup when the module is missing, so a
# base image without FTS5 is a crash-loop rather than a degraded search — and under
# `auto_start_machines` a crash-loop burns compute against a small fixed budget while serving
# nothing. Alpine/musl is rejected for wheel and runtime friction, distroless for having no
# shell to verify any of this with.

FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS builder

# uv from its own published image rather than a pip install: the resolver stays out of the
# runtime stage entirely, so nothing in the final image can invoke it.
COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /bin/uv

ENV UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

# Only the two resolver inputs, and the project itself is NOT installed.
#
# `pyproject.toml` declares packages = ["loom", "server"] with loom -> client/loom, so any
# `pip install .` / `uv sync` that BUILDS the project would drag the entire client — test
# fixtures included — into the image for no runtime benefit. Since server/ imports nothing
# from loom, `--no-install-project` plus an explicit `COPY server/` is smaller, faster, and
# removes a whole class of "why is the client in the server image" question.
#
# The drift risk this buys is dependencies, not files: adding `import redis` to server/*.py
# without adding redis to the `server` extra means it is never installed. That failure is
# loud and immediate — the lifespan import chain raises at process start — and wave m's
# "a container on an empty mount reaches /api/health 200" catches it on the next build.
#
# `--locked`, not `--frozen`: a uv.lock that has drifted from pyproject.toml FAILS here
# rather than silently resolving something new. That is the error worth catching in CI, and
# wave m asserts it by building a deliberately stale context.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --extra server --no-install-project --no-editable


FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

# gosu rather than su/sudo: it execs rather than forking, so the server ends up as PID 1
# and SIGTERM reaches it directly (see CMD).
RUN useradd --system --create-home --home-dir /app app \
 && apt-get update \
 && apt-get install -y --no-install-recommends gosu \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app server/ /app/server/
COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# LOOM_DB must agree with fly.toml's [env] and [mounts].destination. `server/db.py` falls
# back to a CWD-relative ./loom.db when it is unset, and that fallback is the most
# convincing failure in this whole change: the app boots, answers health, and an entry even
# "survives" a restart — on the container's ephemeral disk, having never touched the
# volume. Setting it in the image means a plain `docker run` cannot reach that state either.
ENV PATH="/app/.venv/bin:$PATH" \
    LOOM_DB=/data/loom.db \
    PYTHONUNBUFFERED=1

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# Exec form, NOT shell form. Shell form puts `/bin/sh -c` at PID 1, which does not forward
# SIGTERM: uvicorn would be hard-killed instead of running its graceful shutdown and the
# FastAPI lifespan exit. With `auto_stop_machines` that happens on EVERY idle cycle, not
# rarely. fly.toml's `kill_timeout` (30s) must stay STRICTLY GREATER than the graceful
# window below, not merely equal to it: Fly SIGKILLs `kill_timeout` after SIGTERM, and if
# uvicorn's own drain deadline is the same number, the two land at the same instant and
# the process is killed exactly when it was about to exit cleanly. 25s of drain against a
# 30s kill leaves 5s of headroom for uvicorn to notice the signal, finish the lifespan
# exit and return — and 25s is already generous for a JSON API with no long-lived
# connections. Wave m asserts the headroom, not just the ordering.
#
# One worker: SQLite with WAL allows concurrent readers and a single writer, and three
# engineers on one 256MB Machine have no use for more.
CMD ["python", "-m", "uvicorn", "server.app:app", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--timeout-graceful-shutdown", "25"]
