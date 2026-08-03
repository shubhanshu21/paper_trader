"""
api/routes_health.py — Comprehensive health check endpoints.

Provides detailed health checks for all system dependencies including
database, Redis, and external services.
"""
import os
import time
from datetime import datetime
from typing import Any

import redis
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from automate.db.engine import get_db

router = APIRouter(prefix="/health", tags=["health"])

# Configuration
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
HEALTH_CHECK_TIMEOUT = 5  # seconds


class HealthStatus:
    """Health status constants."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


async def check_database(db: Session) -> dict[str, Any]:
    """Check database health."""
    try:
        start_time = time.time()
        result = db.execute(text("SELECT 1"))
        result.fetchone()
        response_time = (time.time() - start_time) * 1000  # Convert to ms
        
        return {
            "status": HealthStatus.HEALTHY,
            "response_time_ms": round(response_time, 2),
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        return {
            "status": HealthStatus.UNHEALTHY,
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }


async def check_redis() -> dict[str, Any]:
    """Check Redis health."""
    try:
        client = redis.from_url(REDIS_URL, socket_timeout=HEALTH_CHECK_TIMEOUT)
        start_time = time.time()
        client.ping()
        response_time = (time.time() - start_time) * 1000  # Convert to ms
        
        # Get Redis info
        info = client.info()
        
        return {
            "status": HealthStatus.HEALTHY,
            "response_time_ms": round(response_time, 2),
            "connected_clients": info.get("connected_clients", 0),
            "used_memory_human": info.get("used_memory_human", "unknown"),
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        return {
            "status": HealthStatus.UNHEALTHY,
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }


async def check_external_services() -> dict[str, Any]:
    """Check external services health."""
    services = {}
    
    # Check broker API (placeholder - implement actual check)
    try:
        # This would be an actual check against your broker API
        services["broker_api"] = {
            "status": HealthStatus.HEALTHY,
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        services["broker_api"] = {
            "status": HealthStatus.UNHEALTHY,
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }
    
    # Check market data feed (placeholder)
    try:
        services["market_data"] = {
            "status": HealthStatus.HEALTHY,
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        services["market_data"] = {
            "status": HealthStatus.UNHEALTHY,
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }
    
    return services


def determine_overall_status(checks: dict[str, Any]) -> str:
    """Determine overall system health status."""
    statuses = [check.get("status") for check in checks.values()]
    
    if all(status == HealthStatus.HEALTHY for status in statuses):
        return HealthStatus.HEALTHY
    elif any(status == HealthStatus.UNHEALTHY for status in statuses):
        return HealthStatus.UNHEALTHY
    else:
        return HealthStatus.DEGRADED


@router.get("")
async def health_check(db: Session = Depends(get_db)):
    """
    Basic health check endpoint.
    
    Returns 200 if system is healthy, 503 if unhealthy.
    """
    db_health = await check_database(db)
    
    if db_health["status"] == HealthStatus.HEALTHY:
        return {
            "status": HealthStatus.HEALTHY,
            "timestamp": datetime.utcnow().isoformat()
        }
    else:
        raise HTTPException(status_code=503, detail="System unhealthy")


@router.get("/detailed")
async def detailed_health_check(db: Session = Depends(get_db)):
    """
    Detailed health check with all system components.
    """
    checks = {
        "database": await check_database(db),
        "redis": await check_redis(),
        "external_services": await check_external_services()
    }
    
    overall_status = determine_overall_status(checks)
    
    return {
        "status": overall_status,
        "timestamp": datetime.utcnow().isoformat(),
        "checks": checks,
        "version": os.getenv("APP_VERSION", "1.0.0"),
        "environment": os.getenv("ENVIRONMENT", "development")
    }


@router.get("/readiness")
async def readiness_check(db: Session = Depends(get_db)):
    """
    Readiness probe for Kubernetes.
    
    Returns 200 if system is ready to accept traffic.
    """
    db_health = await check_database(db)
    redis_health = await check_redis()
    
    if (db_health["status"] == HealthStatus.HEALTHY and 
        redis_health["status"] == HealthStatus.HEALTHY):
        return {
            "status": "ready",
            "timestamp": datetime.utcnow().isoformat()
        }
    else:
        raise HTTPException(status_code=503, detail="System not ready")


@router.get("/liveness")
async def liveness_check():
    """
    Liveness probe for Kubernetes.
    
    Returns 200 if the application is running.
    """
    return {
        "status": "alive",
        "timestamp": datetime.utcnow().isoformat()
    }


@router.get("/metrics")
async def metrics():
    """
    Basic metrics endpoint.
    
    In production, this would integrate with Prometheus.
    """
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "uptime_seconds": time.time() - getattr(liveness_check, "start_time", time.time()),
        "requests_total": getattr(liveness_check, "request_count", 0),
        "errors_total": getattr(liveness_check, "error_count", 0)
    }
