"""Role-based access control. (§509)

Six roles, as the clause names them, with permissions held as data so granting
one is a row rather than a deployment.

WHAT THIS IS NOT, stated plainly because the distinction matters to anyone
evaluating it: this is not an identity system. There is no password policy, no
lockout, no multi-factor, no SSO, no session management and no token rotation
schedule. A real deployment puts authentication behind the customer's own
directory. What is demonstrated here is that every API call carries a principal,
that the principal has exactly one role, and that the role decides what the call
may do — which is the part §509 is actually about.

Tokens are stored as SHA-256 and shown once. The plaintext is never persisted,
never logged, and cannot be recovered; a lost token is replaced, not looked up.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass

import psycopg

ROLES = ("corporate", "station", "engineering", "operations", "maintenance",
         "admin")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    """A token with 256 bits of entropy from the OS CSPRNG."""
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class Principal:
    username: str
    role: str
    station: str | None
    permissions: frozenset[str]

    def may(self, permission: str) -> bool:
        return permission in self.permissions

    def may_see_station(self, station: str | None) -> bool:
        """The station role is scoped; every other role is fleet-wide."""
        if self.role != "station" or station is None:
            return True
        return self.station == station


class AccessError(PermissionError):
    pass


def create_principal(conn: psycopg.Connection, username: str, role: str, *,
                     full_name: str | None = None, station: str | None = None,
                     actor: str = "admin") -> str:
    """Create a principal and return its token ONCE. It is not stored."""
    if role not in ROLES:
        raise AccessError(f"unknown role {role!r}; expected one of {ROLES}")
    if role == "station" and not station:
        raise AccessError("the station role must be scoped to a station")
    token = new_token()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO principal (username, full_name, role_name, station,"
            " token_sha256) VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (username, full_name, role, station, token_hash(token)))
        principal_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO audit_log (actor, entity, entity_id, field, old_value,"
            " new_value, reason) VALUES (%s,'principal',%s,'role',NULL,%s,%s)",
            (actor, str(principal_id), role, f"created principal {username}"))
    conn.commit()
    return token


def authenticate(conn: psycopg.Connection, token: str) -> Principal:
    """Resolve a bearer token to a principal, or refuse."""
    if not token:
        raise AccessError("no token supplied")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT username, role_name, station, enabled FROM principal"
            " WHERE token_sha256 = %s", (token_hash(token),))
        row = cur.fetchone()
        if row is None:
            # Deliberately the same message as a disabled principal: telling an
            # attacker which tokens exist is free information.
            raise AccessError("unknown or disabled principal")
        username, role, station, enabled = row
        if not enabled:
            raise AccessError("unknown or disabled principal")
        cur.execute("SELECT permission_name FROM role_permission"
                    " WHERE role_name = %s", (role,))
        permissions = frozenset(r[0] for r in cur.fetchall())
        cur.execute("UPDATE principal SET last_seen = now() WHERE username = %s",
                    (username,))
    conn.commit()
    return Principal(username, role, station, permissions)


def require(principal: Principal, permission: str) -> None:
    if not principal.may(permission):
        raise AccessError(
            f"{principal.username} has role {principal.role}, which does not "
            f"carry {permission}")


def revoke(conn: psycopg.Connection, username: str, actor: str = "admin") -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE principal SET enabled = false WHERE username = %s"
                    " RETURNING id", (username,))
        row = cur.fetchone()
        if row is None:
            raise AccessError(f"no such principal: {username}")
        cur.execute(
            "INSERT INTO audit_log (actor, entity, entity_id, field, old_value,"
            " new_value, reason) VALUES (%s,'principal',%s,'enabled','true',"
            "'false','revoked')", (actor, str(row[0])))
    conn.commit()


def main() -> int:
    """Small CLI so access control is usable without writing SQL."""
    import argparse
    ap = argparse.ArgumentParser(prog="ops.access")
    sub = ap.add_subparsers(dest="command", required=True)
    add = sub.add_parser("create")
    add.add_argument("username")
    add.add_argument("role", choices=ROLES)
    add.add_argument("--full-name")
    add.add_argument("--station")
    add.add_argument("--actor", default=os.environ.get("USER", "unknown"))
    rm = sub.add_parser("revoke")
    rm.add_argument("username")
    rm.add_argument("--actor", default=os.environ.get("USER", "unknown"))
    sub.add_parser("list")
    args = ap.parse_args()

    dsn = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")
    with psycopg.connect(dsn) as conn:
        if args.command == "create":
            token = create_principal(conn, args.username, args.role,
                                     full_name=args.full_name,
                                     station=args.station, actor=args.actor)
            print(f"created {args.username} with role {args.role}")
            print(f"token (shown once, not stored): {token}")
        elif args.command == "revoke":
            revoke(conn, args.username, args.actor)
            print(f"revoked {args.username}")
        else:
            with conn.cursor() as cur:
                cur.execute("SELECT username, role_name, station, enabled,"
                            " last_seen FROM principal ORDER BY username")
                print(f"{'username':<16} {'role':<13} {'station':<9} "
                      f"{'enabled':<8} last seen")
                for u, r, s, e, seen in cur.fetchall():
                    print(f"{u:<16} {r:<13} {s or '-':<9} {str(e):<8} "
                          f"{seen or 'never'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
