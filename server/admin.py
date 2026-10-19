"""Server administration CLI: users, invitations and device tokens.

Runs as `python -m server.admin <subcommand>`. The DB path comes from the `LOOM_DB`
environment variable (default `./loom.db`) — the same one app.py uses.

    python -m server.admin invite --email dev2@example.test --name dev2 [--ttl 24h] [--public-url URL]
    python -m server.admin listdevices [--email dev2@example.test]
    python -m server.admin revokedevice --id 3          # any id of the machine's chain
    python -m server.admin revoke-orphans               # once, after upgrading to D19
    python -m server.admin adduser --name dev2 --email dev2@example.test   # break-glass
    python -m server.admin deactivate --email dev2@example.test
    python -m server.admin promote --email dev2@example.test    # D20: the first admin
    python -m server.admin demote --email dev2@example.test
    python -m server.admin listusers

`invite` is the onboarding path (issue #20): it prints a single-use enrolment code that
`loom enroll` exchanges, on the engineer's machine, for that machine's own device token.
When the server's public URL is known (D19) it also prints the invitation link,
`<public-url>/ui/join#code=<code>`, which `loom enroll` takes in place of the bare code.
`adduser` is unchanged — it prints a permanent token and remains the break-glass path.

`promote`/`demote` set ``users.role`` (D20) and are the only commands here that write an
``auth_events`` row, with a ``NULL`` actor. Neither they nor `deactivate` ever leave the
server without an active admin once it has one.
"""
import argparse
import secrets
import sqlite3
import sys

from server import auth, db, public_url


def _adduser(args: argparse.Namespace) -> int:
    conn = db.init_db()
    try:
        token = secrets.token_urlsafe(32)  # 32 bytes of entropy, url-safe
        try:
            cur = conn.execute(
                "INSERT INTO users (name, email, token_hash, active, created_at) VALUES (?, ?, ?, 1, ?)",
                (args.name, args.email, db.hash_token(token), db.iso_now()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            print(f"error: a user with email {args.email!r} already exists", file=sys.stderr)
            return 1
        print(f"User created: id={cur.lastrowid} name={args.name} email={args.email}")
        print()
        print("  TOKEN (shown ONLY ONCE — save it now, it cannot be recovered):")
        print(f"    {token}")
        print()
        print("  Hand it over through a secure channel. On the client, `loom init` writes it to ~/.loom/config.json (chmod 600).")
        return 0
    finally:
        conn.close()


def _last_admin(email: str) -> int:
    print(f"error: {email} is the last active admin (last_admin); promote another user first",
          file=sys.stderr)
    return 1


def _deactivate(args: argparse.Namespace) -> int:
    conn = db.init_db()
    try:
        try:
            result = auth.deactivate(conn, args.email)
        except auth.LastAdmin:
            return _last_admin(args.email)
        if result is None:
            print(f"error: no user with email {args.email!r}", file=sys.stderr)
            return 1
        revoked, expired = result
        print(f"User {args.email} deactivated: {revoked} device token(s) revoked, "
              f"{expired} pending enrolment code(s) expired.")
        print("  A permanent (adduser) token of theirs is refused while they stay deactivated. "
              "There is no reactivate: a returning person is re-created with a new email.")
        return 0
    finally:
        conn.close()


def _set_role(email: str, role: str) -> int:
    conn = db.init_db()
    try:
        user = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if user is None:
            print(f"error: no user with email {email!r}", file=sys.stderr)
            return 1
        try:
            changed = auth.set_role(conn, user["id"], role, audit=auth.CLI_AUDIT)
        except auth.LastAdmin:
            return _last_admin(email)
        if changed:
            print(f"User {email} {'promoted to admin' if role == 'admin' else 'demoted to member'}.")
        else:
            print(f"User {email} is already {'an admin' if role == 'admin' else 'a member'}.")
        return 0
    finally:
        conn.close()


def _promote(args: argparse.Namespace) -> int:
    return _set_role(args.email, "admin")


def _demote(args: argparse.Namespace) -> int:
    return _set_role(args.email, "member")


def _invite(args: argparse.Namespace) -> int:
    conn = db.init_db()
    try:
        invited = auth.invite(conn, args.email, args.name, args.ttl)
        if invited is None:
            print(f"error: no user with email {args.email!r}; pass --name to create one",
                  file=sys.stderr)
            return 1
        _, name, code, expires_at = invited
        print(f"Invitation for {name} ({args.email}): single use, valid until {expires_at}.")
        print()
        print("  ENROLMENT CODE (shown ONLY ONCE — burned when redeemed, useless afterwards):")
        print(f"    {code}")
        print()
        print("  Send it through the team's channel. On the machine, `loom enroll` reads it at a")
        print("  hidden prompt and mints that machine's own device token; the device token never travels.")
        # Appended, never interleaved: every line above stays as callers have parsed it.
        base = public_url.resolve(args.public_url)
        if base is None:
            print(f"note: no invitation link printed, because this server's public URL is unknown. "
                  f"Set {public_url.ENV_VAR} (e.g. https://loom.example.com) in the server's "
                  "environment, or pass --public-url.", file=sys.stderr)
        else:
            print(public_url.join_link(base, code))
        return 0
    finally:
        conn.close()


def _listdevices(args: argparse.Namespace) -> int:
    conn = db.init_db()
    try:
        rows = auth.list_devices(conn, args.email)
        if not rows:
            print("(no devices)")
            return 0
        print(f"{'id':>3}  {'state':<8}  {'email':<30}  {'device':<20}  issued_at             "
              f"expires_at            chain")
        for r in rows:
            print(f"{r['id']:>3}  {auth.device_state(r):<8}  {r['email']:<30}  "
                  f"{r['device_name']:<20}  {r['issued_at']}  {r['expires_at']}  {r['chain']:>5}")
        return 0
    finally:
        conn.close()


def _revokedevice(args: argparse.Namespace) -> int:
    conn = db.init_db()
    try:
        revoked = auth.revoke(conn, args.id)
        if not revoked:
            print(f"error: no unrevoked device token in the chain of id {args.id}", file=sys.stderr)
            return 1
        print(f"Device token {args.id} and its renewal chain revoked ({revoked} token(s); "
              "that machine must enrol again).")
        return 0
    finally:
        conn.close()


def _revoke_orphans(args: argparse.Namespace) -> int:
    conn = db.init_db()
    try:
        revoked = auth.revoke_orphans(conn)
        if not revoked:
            print("No orphaned device tokens.")
            return 0
        branches: dict[int, list[int]] = {}
        for root, device_id in revoked:
            branches.setdefault(root, []).append(device_id)
        print(f"Revoked {len(revoked)} orphaned device token(s), by the chain they hung from:")
        for root, ids in branches.items():
            print(f"  chain {root}: {' '.join(str(i) for i in ids)}")
        print("  A machine that was still using one of them gets a 401 and re-enrols.")
        return 0
    finally:
        conn.close()


def _listusers(args: argparse.Namespace) -> int:
    conn = db.init_db()
    try:
        rows = conn.execute(
            "SELECT id, name, email, active, created_at, role FROM users ORDER BY id"
        ).fetchall()
        if not rows:
            print("(no users)")
            return 0
        # `role` (D20) is appended: every column before it stays where callers parse it.
        print(f"{'id':>3}  {'active':<6}  {'name':<20}  {'email':<30}  {'created_at':<20}  role")
        for r in rows:
            state = "yes" if r["active"] else "no"
            print(f"{r['id']:>3}  {state:<6}  {r['name']:<20}  {r['email']:<30}  "
                  f"{r['created_at']:<20}  {r['role']}")
        return 0
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m server.admin",
        description="Synaptic Loom user administration (DB at $LOOM_DB, default ./loom.db).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("adduser", help="create a user and print its token once")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--email", required=True)
    p_add.set_defaults(func=_adduser)

    p_deact = sub.add_parser("deactivate", help="deactivate a user: revoke their machines, expire their pending codes "
                                    "(never the last active admin)")
    p_deact.add_argument("--email", required=True)
    p_deact.set_defaults(func=_deactivate)

    p_promote = sub.add_parser("promote", help="make a user an admin (bootstraps the first admin)")
    p_promote.add_argument("--email", required=True)
    p_promote.set_defaults(func=_promote)

    p_demote = sub.add_parser("demote", help="make an admin a member (never the last active admin)")
    p_demote.add_argument("--email", required=True)
    p_demote.set_defaults(func=_demote)

    p_list = sub.add_parser("listusers", help="list users")
    p_list.set_defaults(func=_listusers)

    p_invite = sub.add_parser(
        "invite", help="print a single-use enrolment code (creates the user with --name)"
    )
    p_invite.add_argument("--email", required=True)
    p_invite.add_argument("--name", help="required when the user does not exist yet")
    p_invite.add_argument(
        "--ttl", type=auth.parse_ttl, default=auth.parse_ttl(auth.DEFAULT_ENROL_CODE_TTL),
        help="code lifetime, e.g. 30m, 24h (default), 3d",
    )
    p_invite.add_argument(
        "--public-url",
        help=f"the server's URL for the invitation link (default ${public_url.ENV_VAR}, "
             "else https://$FLY_APP_NAME.fly.dev)",
    )
    p_invite.set_defaults(func=_invite)

    p_devices = sub.add_parser("listdevices", help="list device tokens (never the tokens)")
    p_devices.add_argument("--email", help="only this user's devices")
    p_devices.set_defaults(func=_listdevices)

    p_revoke = sub.add_parser("revokedevice", help="revoke ONE machine: its whole renewal chain")
    p_revoke.add_argument("--id", type=int, required=True,
                          help="any device id of the machine's chain, from listdevices")
    p_revoke.set_defaults(func=_revokedevice)

    p_orphans = sub.add_parser(
        "revoke-orphans",
        help="revoke the live device tokens no renewal chain reaches (left by pre-D19 renewals)",
    )
    p_orphans.set_defaults(func=_revoke_orphans)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
