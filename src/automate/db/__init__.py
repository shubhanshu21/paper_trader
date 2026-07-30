"""
db/__init__.py — Exports engine, SessionLocal, and Base for the whole app.
"""
from automate.db.engine import engine, SessionLocal, Base

__all__ = ["engine", "SessionLocal", "Base"]
