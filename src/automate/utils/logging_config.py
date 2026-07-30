"""
utils/logging_config.py — Structured logging configuration.

Provides production-grade structured logging with JSON format,
log levels, and proper log rotation.
"""
import logging
import logging.config
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Any
import json


class JSONFormatter(logging.Formatter):
    """JSON formatter for structured logging."""
    
    def format(self, record: logging.LogRecord) -> str:
        log_data: Dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        
        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        # Add extra fields
        if hasattr(record, "extra"):
            log_data.update(record.extra)
        
        return json.dumps(log_data)


def setup_logging(
    level: str = os.getenv("LOG_LEVEL", "INFO"),
    log_dir: str = os.getenv("LOG_DIR", "logs"),
    enable_json: bool = os.getenv("LOG_JSON", "true").lower() == "true"
) -> None:
    """Setup structured logging configuration."""
    
    # Create log directory if it doesn't exist
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    
    # Convert string level to logging constant
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    
    logging_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S"
            },
            "json": {
                "()": "automate.utils.logging_config.JSONFormatter"
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "stream": sys.stdout,
                "formatter": "json" if enable_json else "default",
                "level": numeric_level
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": log_path / "app.log",
                "maxBytes": 10485760,  # 10MB
                "backupCount": 5,
                "formatter": "json" if enable_json else "default",
                "level": numeric_level
            },
            "error_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": log_path / "error.log",
                "maxBytes": 10485760,  # 10MB
                "backupCount": 5,
                "formatter": "json" if enable_json else "default",
                "level": logging.ERROR
            }
        },
        "loggers": {
            "": {  # Root logger
                "handlers": ["console", "file", "error_file"],
                "level": numeric_level,
                "propagate": False
            },
            "uvicorn": {
                "handlers": ["console"],
                "level": logging.INFO,
                "propagate": False
            },
            "uvicorn.access": {
                "handlers": ["console"],
                "level": logging.INFO,
                "propagate": False
            },
            "sqlalchemy": {
                "handlers": ["console"],
                "level": logging.WARNING,
                "propagate": False
            }
        }
    }
    
    logging.config.dictConfig(logging_config)


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance with the given name."""
    return logging.getLogger(name)
