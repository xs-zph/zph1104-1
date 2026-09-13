"""Vercel serverless entrypoint for the FastAPI application."""

from app.main import app

__all__ = ["app"]
