# Security policy

## Reporting a vulnerability

Please report it privately through GitHub:
**[Report a vulnerability](https://github.com/DiegoSanC/synaptic-loom/security/advisories/new)**
(the repository's *Security* tab → *Report a vulnerability*).

Do not open a public issue for a vulnerability. Include what you found, how to reproduce
it, and the commit or version you tested.

This is a small project maintained on a best-effort basis. Reports are read and answered,
and a fix is coordinated with you before anything is disclosed.

## Supported versions

Only the latest release and `main` receive fixes.

## What is in scope

Synaptic Loom moves a team's knowledge between machines, so these matter most:

- **Authentication and authorization on the server:** device tokens, enrolment codes,
  setup codes, the web UI session, roles, and the revocation cascade (`DESIGN.md` §6).
- **Anything that makes the client upload more than the summary and its redacted
  extract:** a raw transcript, a token, a secret that gets past the gitleaks checks, an
  extract stored for a project switched off, or a stored extract that search can reach
  (`docs/USER-GUIDE.md`, "What actually leaves your machine"). What a client plugin you
  installed sends is that plugin's own (D23); a way for a plugin to reach more than D23
  hands it, or to cost or change a capture, is in scope.
- **Attribution:** a session filed under the wrong project (`contract/ATTRIBUTION.md`).
- **The deployment guides:** a setting in `fly.toml` or `deploy/compose/` that exposes
  the server or lets a client choose its own address (`docs/SERVER-GUIDE.md`).

Content of the logbook is out of scope: entries are machine-written summaries and are
presented as unverified reference by design.
