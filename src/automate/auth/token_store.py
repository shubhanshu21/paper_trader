"""
auth/token_store.py — Upstox access token persistence, backed by MySQL
instead of .env.

Replaces the old scheme where the daily-refreshed OAuth token lived as a
plaintext line in .env: that file was shared on disk between two independent
OS processes (automate-api, run_daemon.py), each of which cached its own
in-memory copy at startup — refreshing the token in one process never
propagated to the other without a manual restart. Storing it in the same
MySQL DB both processes already talk to fixes that class of staleness and
keeps the credential out of a general-purpose config file.
"""
import logging
from typing import Optional

from sqlalchemy import select

from automate.db.engine import get_session
from automate.db.models import BrokerToken

log = logging.getLogger("auth.token_store")

DEFAULT_BROKER = "upstox"


def get_access_token(broker: str = DEFAULT_BROKER) -> Optional[str]:
    try:
        with get_session() as session:
            row = session.get(BrokerToken, broker)
            return row.access_token if row else None
    except Exception:
        log.exception("Could not read %s access token from DB.", broker)
        return None


def set_access_token(token: str, broker: str = DEFAULT_BROKER) -> None:
    with get_session() as session:
        row = session.get(BrokerToken, broker)
        if row is None:
            row = BrokerToken(broker=broker, access_token=token)
            session.add(row)
        else:
            row.access_token = token
        session.commit()
    log.info("%s access token updated in DB.", broker)
