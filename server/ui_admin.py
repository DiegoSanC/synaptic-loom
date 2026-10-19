"""*Admin → Users* (D20, DESIGN §6.4; plan 2026-10-19, phase 2 slice 4).

``GET /ui/admin/users`` and its writes — invite, deactivate, change role — registered on
``ui_pages.router`` through ``_page``/``_write``, so the session gate, the write registry
and the D14 crawl see them like every other page. ``server.app`` imports this module
before it includes that router.

Admins only: every handler is wrapped by ``_admins_only``, which reads the role the gate
put on ``request.state`` (from ``users``, on every request) before anything else runs, so
a member learns nothing from an id and changes nothing. Each write runs in
``ui_pages.write_transaction``, which reads that role again under the write lock.
"""
from __future__ import annotations

import functools

from fastapi import Request
from starlette.responses import HTMLResponse, RedirectResponse

from server import auth, db, public_url
from server.ui_pages import (_page, _write, message, not_found, refusal, render,
                             write_transaction)

ADMIN_USERS = "/ui/admin/users"
INVITE_TTL_S = auth.parse_ttl(auth.DEFAULT_ENROL_CODE_TTL)


def _admins_only(handler):
    @functools.wraps(handler)
    def guarded(request: Request, **kwargs):
        if request.state.user_role != "admin":
            return refusal(403, "forbidden", request=request)
        return handler(request, **kwargs)
    return guarded


def _audit(request: Request) -> dict:
    """The audit context of the account functions of ``auth``: this admin, this address."""
    return {"actor_user_id": request.state.user_id, "client_ip": request.state.client_ip}


def _back() -> RedirectResponse:
    return RedirectResponse(ADMIN_USERS, status_code=303)


def _last_admin(request: Request, name: str) -> HTMLResponse:
    return message(409, "Last admin", f"{name} is the last active admin (last_admin): "
                   "promote another user first.", request=request)


# --------------------------------------------------------------------------- #
# Queries                                                                      #
# --------------------------------------------------------------------------- #
def _users(conn) -> list[dict]:
    """Every user with their state, a pending-invitation flag and their machines (one
    per renewal chain, newest token's state, named by the chain's first id)."""
    now = db.iso_now()
    users = [dict(row, machines=[]) for row in conn.execute(
        "SELECT u.id, u.name, u.email, u.role, u.active, EXISTS ("
        " SELECT 1 FROM enrolment_codes c WHERE c.user_id = u.id AND c.kind = 'invite'"
        " AND c.redeemed_at IS NULL AND c.expires_at > ?) AS pending "
        "FROM users u ORDER BY u.id", (now,))]
    by_email = {user["email"]: user for user in users}
    chains: dict[int, list] = {}
    for row in auth.list_devices(conn):
        chains.setdefault(row["chain"], []).append(row)
    for root, rows in chains.items():
        tail = next((row for row in reversed(rows) if row["replaced_by"] is None), rows[-1])
        by_email[tail["email"]]["machines"].append({
            "id": root, "name": tail["device_name"], "state": auth.device_state(tail, now),
            "enrolled": rows[0]["issued_at"], "expires": tail["expires_at"]})
    return users


# --------------------------------------------------------------------------- #
# Routes                                                                       #
# --------------------------------------------------------------------------- #
@_page(ADMIN_USERS)
@_admins_only
def users_page(request: Request):
    conn = db.connect()
    try:
        users = _users(conn)
    finally:
        conn.close()
    return render("admin_users.html", request, users=users, roles=auth.ROLES)


@_write(f"{ADMIN_USERS}/invite")
@_admins_only
def invite(request: Request):
    """``admin.py invite``'s semantics, issued by this session: 24 h, kind ``invite``,
    the admin and the session's device token as its issuer; the link is shown once."""
    form = request.state.form
    email, name, role = (form.get(field, "").strip() for field in ("email", "name", "role"))
    if not email:
        return message(400, "Email needed", "An invitation needs the person's email.",
                       request=request)
    if role not in auth.ROLES:
        return message(400, "Role needed", "The role must be admin or member.",
                       request=request)
    conn = db.connect()
    try:
        with write_transaction(request, conn):
            invited = auth.invite(conn, email, name or None, INVITE_TTL_S, role=role,
                                  created_by_user_id=request.state.user_id,
                                  issued_by_device_token_id=request.state.ui_device_token_id,
                                  audit=_audit(request))
    finally:
        conn.close()
    if invited is None:
        return message(400, "Name needed", f"No user has the email {email}: give a name "
                       "to create one.", request=request)
    _, name, code, expires_at = invited
    # The gate refuses every write while no public URL resolves, so there is one here.
    return render("invite_link.html", request, invitee=name, email=email,
                  link=public_url.join_link(public_url.resolve(), code), expires_at=expires_at)


@_write(ADMIN_USERS + "/{user_id:int}/deactivate")
@_admins_only
def deactivate(request: Request, user_id: int):
    """``admin.py deactivate``'s effects (``auth.deactivate``) and ``user_deactivated``,
    for a user still active under the write lock; an inactive one is left as it is."""
    if not db.is_row_id(user_id):
        return not_found(request)
    conn = db.connect()
    try:
        with write_transaction(request, conn):
            user = conn.execute("SELECT name, email, active FROM users WHERE id = ?",
                                (user_id,)).fetchone()
            if user is None:
                return not_found(request)
            if user["active"]:
                auth.deactivate(conn, user["email"], audit=_audit(request))
    except auth.LastAdmin:
        return _last_admin(request, user["name"])
    finally:
        conn.close()
    return _back()


@_write(ADMIN_USERS + "/{user_id:int}/role")
@_admins_only
def change_role(request: Request, user_id: int):
    """``auth.set_role``: ``promoted``/``demoted`` in its transaction, never the last
    active admin."""
    role = request.state.form.get("role", "")
    if role not in auth.ROLES:
        return message(400, "Role needed", "The role must be admin or member.",
                       request=request)
    if not db.is_row_id(user_id):
        return not_found(request)
    conn = db.connect()
    try:
        try:
            with write_transaction(request, conn):
                changed = auth.set_role(conn, user_id, role, audit=_audit(request))
        except auth.LastAdmin:
            name = conn.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()
            return _last_admin(request, name["name"])
    finally:
        conn.close()
    return not_found(request) if changed is None else _back()
