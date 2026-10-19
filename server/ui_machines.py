"""*My machines*, setup codes and the web's revoke (D20, DESIGN §6.4; plan 2026-10-19,
phase 2 slice 3).

Every route is registered on ``ui_pages.router`` when this module is imported (so the
write registry and the D14 crawl see it) and reached through ``app._ui_session_gate``:
the page is a signed-in ``GET``, and the two writes are registered with
``ui_pages._write``, so the gate has already checked the public URL, ``Origin``, the CSRF
token and the session's kind before a handler runs, and each write runs in
``ui_pages.write_transaction``, which re-reads the session under the write lock. A machine
is a renewal chain, the connected component over ``replaced_by`` that
``auth.revoke_chain`` revokes; any id of it names it.
"""
from __future__ import annotations

import sqlite3

from fastapi import Request
from starlette.responses import RedirectResponse

from server import auth, auth_events, db, public_url
from server.ui_pages import _page, _write, not_found, render, write_transaction


def chains(conn: sqlite3.Connection, user_id: int, now: str) -> list[dict]:
    """One dict per chain of ``user_id``, oldest first: ``id`` (its first token, which
    names it in the Revoke form), ``name``, ``created`` and ``renewed`` (the first and the
    newest ``issued_at``), ``expires`` and ``state`` (``live``, ``expired`` or
    ``revoked``), all three of the chain's head — the token nothing replaced."""
    rows = conn.execute(
        "SELECT id, device_name, issued_at, expires_at, revoked_at, replaced_by "
        "FROM device_tokens WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()
    by_id = {row["id"]: row for row in rows}
    members: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        head, walked = row, {row["id"]}
        while head["replaced_by"] in by_id and head["replaced_by"] not in walked:
            head = by_id[head["replaced_by"]]
            walked.add(head["id"])
        members.setdefault(head["id"], []).append(row)
    out = []
    for head_id, tokens in members.items():
        head = by_id[head_id]
        if head["revoked_at"] is not None:
            state = "revoked"
        elif head["expires_at"] <= now:
            state = "expired"
        else:
            state = "live"
        out.append({"id": min(t["id"] for t in tokens),
                    "ids": {t["id"] for t in tokens},
                    "name": head["device_name"],
                    "created": min(t["issued_at"] for t in tokens),
                    "renewed": max(t["issued_at"] for t in tokens),
                    "expires": head["expires_at"], "state": state})
    return sorted(out, key=lambda chain: chain["id"])


def _ttl_text(seconds: int) -> str:
    return f"{seconds // 60} minutes" if seconds % 60 == 0 else f"{seconds} seconds"


@_page("/ui/machines")
def machines_page(request: Request):
    """*My machines*: the caller's chains, "Add a machine", and "Revoke" per live chain."""
    conn = db.connect()
    try:
        rows = chains(conn, request.state.user_id, db.iso_now())
    finally:
        conn.close()
    return render("machines.html", request, chains=rows,
                  this_device=request.state.ui_device_token_id,
                  ttl=_ttl_text(auth.setup_code_ttl_s()))


@_write("/ui/machines/setup-code")
def issue_setup_code(request: Request):
    """A single-use setup code for one more machine of the caller's, shown once. It names
    the device token of this session, which the issuer cascade follows."""
    user_id = request.state.user_id
    conn = db.connect()
    try:
        with write_transaction(request, conn):
            code, expires_at = auth.create_invitation(
                conn, user_id, auth.setup_code_ttl_s(), kind="setup",
                created_by_user_id=user_id,
                issued_by_device_token_id=request.state.ui_device_token_id)
            auth_events.record(conn, actor_user_id=user_id, event="setup_code_issued",
                               target_type="user", target_id=user_id,
                               client_ip=request.state.client_ip)
    finally:
        conn.close()
    # The gate refused every write already when the public URL does not resolve.
    link = public_url.join_link(public_url.resolve(), code)
    return render("setup_code.html", request, code=code, link=link, expires_at=expires_at)


@_write("/ui/devices/{device_id:int}/revoke")
def revoke_device(request: Request, device_id: int):
    """Revoke the chain ``device_id`` belongs to, with the issuer cascade: the caller's
    own, or anyone's for an admin; any other id is a 404, unknown or not."""
    if not db.is_row_id(device_id):
        return not_found(request)
    user_id = request.state.user_id
    conn = db.connect()
    try:
        with write_transaction(request, conn):
            row = conn.execute("SELECT user_id FROM device_tokens WHERE id = ?",
                               (device_id,)).fetchone()
            # The role `write_transaction` read under the lock, not the gate's.
            if row is None or (row["user_id"] != user_id
                               and request.state.user_role != "admin"):
                return not_found(request)
            if auth.revoke_chain(conn, device_id, db.iso_now()):
                auth_events.record(conn, actor_user_id=user_id, event="device_revoked",
                                   target_type="device", target_id=device_id,
                                   client_ip=request.state.client_ip)
    finally:
        conn.close()
    own = row["user_id"] == user_id
    return RedirectResponse("/ui/machines" if own else "/ui/admin/users", status_code=303)
