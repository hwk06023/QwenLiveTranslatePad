"""Vercel Python entrypoint.

Vercel serves public/ from its CDN and rewrites /api/* here. The cloud API
engine works as-is; the on-prem engine reports itself unavailable because
torch is not installed in the serverless bundle.
"""

from app.main import app

__all__ = ["app"]
