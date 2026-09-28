"""Delete an account whose owner lost both the password and the recovery key.

Run inside the backend container, after checking by e-mail that the request
really comes from the address of the account:

    docker exec -it capitalview-backend python -m scripts.delete_account --email x@y.fr

Without --confirm it only shows the account. With it, the address has to be
typed back before anything is deleted.
"""

import argparse
import sys
from collections.abc import Callable

from sqlmodel import Session, select

from database import get_engine
from dtos.auth import normalize_email
from models.user import User
from services.account_data import purge_account_without_key


def run(session: Session, email: str, confirm: bool, ask: Callable[[str], str] = input) -> int:
    email = normalize_email(email)
    user = session.exec(select(User).where(User.email == email)).first()
    if user is None:
        print(f"Aucun compte pour {email}.")
        return 1

    print(f"Compte : {user.username} <{user.email}>, créé le {user.created_at:%d/%m/%Y}.")
    if not confirm:
        print("Rien n'a été supprimé. Relancez avec --confirm pour le supprimer.")
        return 0

    if normalize_email(ask("Retapez l'adresse pour supprimer le compte définitivement : ")) != email:
        print("L'adresse ne correspond pas, rien n'a été supprimé.")
        return 1

    deleted = purge_account_without_key(session, user)
    print("Supprimé : " + ", ".join(f"{table} {count}" for table, count in sorted(deleted.items())))
    print("Ses données chiffrées restent en base, illisibles et rattachées à personne.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", required=True, help="adresse du compte à supprimer")
    parser.add_argument("--confirm", action="store_true", help="supprimer pour de bon")
    args = parser.parse_args(argv)
    with Session(get_engine()) as session:
        return run(session, args.email, args.confirm)


if __name__ == "__main__":
    sys.exit(main())
