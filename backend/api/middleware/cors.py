"""
api/middleware/cors.py — CORS configuration middleware.

Provides Cross-Origin Resource Sharing configuration for secure
cross-origin requests.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config.environment import get_settings


def setup_cors(app: FastAPI):
    """
    Setup CORS middleware for the FastAPI app.
    
    Configures CORS based on environment settings with appropriate
    security restrictions for production.
    """
    settings = get_settings()
    api_config = settings.api
    
    # Get allowed origins from configuration
    # Production: strict CORS. Development: allow all origins for convenience.
    allowed_origins = api_config.CORS_ORIGINS if settings.is_production else ["*"]
    
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=api_config.CORS_ALLOW_CREDENTIALS,
        allow_methods=api_config.CORS_ALLOW_METHODS,
        allow_headers=api_config.CORS_ALLOW_HEADERS,
        max_age=600,  # Cache preflight requests for 10 minutes
    )
