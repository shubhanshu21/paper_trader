"""
tests/conftest.py — Shared pytest fixtures.

`automate` is installed editable (pip install -e . — see pyproject.toml),
so `from automate.X import Y` resolves normally with no sys.path hack
needed here, unlike before the src/automate/ restructure.

Test DB: a dedicated 'automate_test' MySQL schema (NEVER the real
'automate' production schema — this project's standing rule against
letting tests touch production data) on the same local MySQL instance
the app itself uses in prod/dev. This app is MySQL-only end to end now —
SQLite was removed everywhere, including test fixtures, which previously
used sqlite:///:memory: as a lightweight stand-in. All tables are created
once per test session via Base.metadata.create_all(); each individual
test then runs inside its own transaction that's rolled back at the end
(db_session fixture below), so tests stay fast and fully isolated from
each other without re-creating the schema per test.

One-time setup (already done on this machine, documented here for a
fresh environment):
    CREATE DATABASE automate_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
    GRANT ALL PRIVILEGES ON automate_test.* TO '<DB_USER>'@'localhost';
"""
import urllib.parse

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import automate.db.models  # noqa: F401 — import registers every model's table on Base.metadata before create_all() below
from automate.config import DatabaseConfig as db_config
from automate.db.engine import Base

TEST_DB_NAME = "automate_test"


def _test_db_url() -> str:
    # Same URL shape as DatabaseConfig.url(), with NAME swapped to the
    # dedicated test schema — quote_plus is required here (not just an
    # f-string) since the real DB password contains reserved URL
    # characters ('@') that would otherwise break connection parsing.
    pwd = urllib.parse.quote_plus(db_config.PASSWORD)
    return f"mysql+pymysql://{db_config.USER}:{pwd}@{db_config.HOST}:{db_config.PORT}/{TEST_DB_NAME}?charset=utf8mb4"


@pytest.fixture(scope="session")
def _test_engine():
    engine = create_engine(_test_db_url(), pool_pre_ping=True, hide_parameters=True)
    # drop_all + create_all (not just create_all) so the schema always
    # matches the CURRENT models.py exactly — automate_test is a persistent
    # schema across test runs, and create_all alone only adds missing
    # tables, never missing/changed columns on ones that already exist
    # from a previous run against an older model shape. Safe to drop
    # unconditionally: this is the dedicated test schema, never production.
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session_factory(_test_engine):
    """
    A sessionmaker bound to ONE connection + ONE outer transaction for the
    whole test — every Session created from it (call it as many times as
    needed) sees the same uncommitted data, the same role the old
    sqlite:///:memory: engine played for tests that hand a "SessionLocal"
    factory to code-under-test via monkeypatch. Everything is rolled back
    when the test ends, so automate_test never accumulates test data.

    Uses the standard SQLAlchemy "join a Session into an external
    transaction" recipe (SAVEPOINT-based, restarted via the
    after_transaction_end event) rather than a bare outer transaction:
    some code-under-test calls session.rollback() itself on an error path
    (e.g. routes_custom_strategies.py::_run_backtest_sync's failure
    branch) — with a bare shared connection, that rollback() would revert
    the REAL outer transaction (since every session shares one physical
    DBAPI connection), wiping out data earlier parts of the SAME test
    already committed and leaving the connection's transaction state
    desynced from this fixture's own bookkeeping. A SAVEPOINT confines
    each session's own commit/rollback to itself.
    """
    connection = _test_engine.connect()
    outer_transaction = connection.begin()
    connection.begin_nested()  # SAVEPOINT every session's own commit()/rollback() ends, instead of the outer transaction

    # expire_on_commit=False matches automate.db.engine.SessionLocal's own
    # config (and every old sqlite fixture this replaces) — several tests
    # commit in one session/fixture (e.g. a seed helper that closes its
    # session right after) and then read attributes off those same ORM
    # objects afterward; with the default expire_on_commit=True that
    # access would try to re-fetch from an already-closed/detached
    # session and raise DetachedInstanceError.
    factory = sessionmaker(bind=connection, expire_on_commit=False)

    @event.listens_for(factory, "after_transaction_end")
    def _restart_savepoint(session, transaction):
        # Every Session.commit()/rollback() ends the current SAVEPOINT —
        # immediately reopen one so the NEXT commit/rollback (this test's
        # own, or code-under-test's) is still confined to a SAVEPOINT
        # rather than falling through to the real outer transaction.
        if transaction.nested and not transaction._parent.nested:
            session.begin_nested()

    yield factory
    event.remove(factory, "after_transaction_end", _restart_savepoint)
    outer_transaction.rollback()
    connection.close()


@pytest.fixture()
def db_session(db_session_factory):
    """One ready-to-use Session for tests that only need a single one — see db_session_factory for the multi-session case."""
    session = db_session_factory()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _never_touch_real_notifications(db_session_factory, monkeypatch):
    """
    Global safety net, applied to EVERY test automatically — no test file
    needs to remember this itself.

    utils/notify.py writes an in-app Notification row via a LOCAL `from
    automate.db.engine import SessionLocal` (re-resolved fresh on every
    call, not a fixed import), and also fires a real Telegram alert. Any
    test that exercises a real failure/kill-switch code path (e.g.
    BaseStrategy's exception handler, RuleBasedStrategy's partial-fill
    unwind) — even one that has nothing to do with notifications as its
    actual subject — ends up calling the REAL notify() unless the test
    explicitly mocks it. This actually happened: test_partial_fill_auto_unwind.py
    (pre-existing, testing an unrelated unwind bug) silently wrote ~240
    fake "TESTSTOCK kill switch" rows into the PRODUCTION notifications
    table and fired real Telegram alerts to the operator's phone, every
    time the suite ran, across multiple sessions — discovered only when
    they showed up in the live notification bell.

    Patching automate.db.engine.SessionLocal here redirects any such
    call to this test's own automate_test transaction (rolled back same
    as everything else) instead of production, and patching
    send_telegram_alert prevents the real HTTP call outright — both
    apply regardless of which module actually calls notify(), and
    regardless of whether that test file remembered to mock anything.
    Individual tests that want to assert notify() WAS called still
    patch it directly at their own call site (e.g. `sched.notify`) —
    that continues to work exactly as before; this fixture only affects
    the path notify() itself would otherwise take under the hood.
    """
    import sys

    import automate.utils.telegram_alert as telegram_alert
    # automate/db/__init__.py does `from .engine import engine`, which
    # shadows the `automate.db.engine` package ATTRIBUTE with the Engine
    # instance itself — `import automate.db.engine as db_engine` would
    # bind to the Engine, not the module. Go through sys.modules instead,
    # which always holds the real submodule regardless (see this same fix
    # already applied in test_wallet_real_margin.py etc.).
    db_engine = sys.modules["automate.db.engine"]
    monkeypatch.setattr(db_engine, "SessionLocal", db_session_factory)
    # Patches the actual network call (requests.post), not
    # send_telegram_alert itself — test_telegram_alert.py's own tests
    # exercise send_telegram_alert directly and patch requests.post
    # themselves; since that happens inside the test body (after this
    # autouse fixture's setup already ran), their own patch simply
    # overrides this one for those specific tests, same as before. Returns
    # a fake 200 response object (not None) — send_telegram_alert reads
    # resp.status_code unconditionally, and would crash on a bare None.
    class _FakeTelegramResponse:
        status_code = 200
        text = "ok"

    monkeypatch.setattr(telegram_alert.requests, "post", lambda *a, **k: _FakeTelegramResponse())
