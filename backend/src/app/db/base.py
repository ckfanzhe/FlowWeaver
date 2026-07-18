"""SQLAlchemy 2.0 declarative base."""
from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    """Project-wide declarative base."""
    pass