"""Admin commands run inside the container, e.g.
docker exec fleet-web python -m fleet_web.cli create-admin you@example.com"""

import sys

from . import store


def main(args):
    if len(args) != 2 or args[0] not in ("create-admin", "reset-password"):
        sys.exit("usage: python -m fleet_web.cli create-admin|reset-password <email>")
    command, email = args[0], args[1].strip().lower()
    db = store.connect()
    if command == "create-admin":
        password = store.create_user(db, email, is_admin=True)
    else:
        row = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if row is None:
            sys.exit(f"no account for {email}")
        password = store.reset_password(db, row["id"])
    store.audit(db, "cli", command, email)
    print(f"{email}\none-time password: {password}\n(must be changed at first sign-in)")


if __name__ == "__main__":
    main(sys.argv[1:])
