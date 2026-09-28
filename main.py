"""Vercel/FastAPI entrypoint.

The application implementation lives in app.main so local uvicorn and Vercel
use the same FastAPI instance.
"""

from app.main import app

__all__ = ["app"]
