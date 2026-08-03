"""
db/__init__.py — Exports engine, SessionLocal, and Base for the whole app.
"""
from automate.db.engine import Base, SessionLocal, engine

__all__ = ["Base", "SessionLocal", "engine"]
