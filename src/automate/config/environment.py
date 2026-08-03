"""
config/environment.py — Environment-based configuration management.

Provides production-grade configuration management with environment
variable loading, validation, and type safety.
"""
import os
from enum import StrEnum

from pydantic.v1 import BaseSettings, Field, validator


class Environment(StrEnum):
    """Environment types."""
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    TEST = "test"


class DatabaseConfig(BaseSettings):
    """Database configuration."""
    
    # Connection settings
    DB_HOST: str = Field(default="localhost", env="DB_HOST")
    DB_PORT: int = Field(default=3306, env="DB_PORT")
    DB_NAME: str = Field(default="automate", env="DB_NAME")
    DB_USER: str = Field(default="root", env="DB_USER")
    DB_PASSWORD: str = Field(default="", env="DB_PASSWORD")
    
    # Connection pool settings
    DB_POOL_SIZE: int = Field(default=10, env="DB_POOL_SIZE")
    DB_MAX_OVERFLOW: int = Field(default=20, env="DB_MAX_OVERFLOW")
    DB_POOL_TIMEOUT: int = Field(default=30, env="DB_POOL_TIMEOUT")
    DB_POOL_RECYCLE: int = Field(default=3600, env="DB_POOL_RECYCLE")
    
    @property
    def database_url(self) -> str:
        """Get database connection URL."""
        import urllib.parse
        pwd = urllib.parse.quote_plus(self.DB_PASSWORD)
        return f"mysql+pymysql://{self.DB_USER}:{pwd}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
    
    def validate(self) -> list[str]:
        """Validate database configuration."""
        errors = []
        if not self.DB_USER:
            errors.append("DB_USER is required")
        return errors

    class Config:
        env_prefix = "DB_"


class RedisConfig(BaseSettings):
    """Redis configuration."""
    
    REDIS_HOST: str = Field(default="localhost", env="REDIS_HOST")
    REDIS_PORT: int = Field(default=6379, env="REDIS_PORT")
    REDIS_DB: int = Field(default=0, env="REDIS_DB")
    REDIS_PASSWORD: str | None = Field(default=None, env="REDIS_PASSWORD")
    REDIS_MAX_CONNECTIONS: int = Field(default=50, env="REDIS_MAX_CONNECTIONS")
    
    @property
    def redis_url(self) -> str:
        """Get Redis connection URL."""
        if self.REDIS_PASSWORD:
            return f"redis://:{self.REDIS_PASSWORD}@{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"
    
    class Config:
        env_prefix = "REDIS_"


class SecurityConfig(BaseSettings):
    """Security configuration."""
    
    # JWT settings
    JWT_SECRET_KEY: str = Field(default="change-this-in-production", env="JWT_SECRET_KEY")
    JWT_ALGORITHM: str = Field(default="HS256", env="JWT_ALGORITHM")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=30, env="ACCESS_TOKEN_EXPIRE_MINUTES")
    REFRESH_TOKEN_EXPIRE_DAYS: int = Field(default=7, env="REFRESH_TOKEN_EXPIRE_DAYS")
    
    # CSRF settings
    CSRF_SECRET: str = Field(default="change-this-in-production", env="CSRF_SECRET")
    CSRF_ENABLED: bool = Field(default=True, env="CSRF_ENABLED")
    CSRF_HEADER: str = Field(default="X-CSRF-Token", env="CSRF_HEADER")
    
    # Rate limiting
    RATE_LIMIT_ENABLED: bool = Field(default=True, env="RATE_LIMIT_ENABLED")
    DEFAULT_RATE_LIMIT: str = Field(default="200/minute", env="DEFAULT_RATE_LIMIT")
    
    @validator("JWT_SECRET_KEY", "CSRF_SECRET")
    def validate_secrets(cls, v):
        """Validate that secrets are not default in production."""
        env = os.getenv("ENVIRONMENT", "development")
        if env == "production" and v in ["change-this-in-production", "your-secret-key-change-in-production"]:
            raise ValueError("Secret keys must be changed in production")
        return v
    
    class Config:
        env_prefix = "SECURITY_"


class BrokerConfig(BaseSettings):
    """Broker API configuration."""
    
    BROKER_API_KEY: str = Field(default="", env="BROKER_API_KEY")
    BROKER_API_SECRET: str = Field(default="", env="BROKER_API_SECRET")
    BROKER_REDIRECT_URL: str = Field(default="http://localhost:8000", env="BROKER_REDIRECT_URL")
    BROKER_ENVIRONMENT: str = Field(default="paper", env="BROKER_ENVIRONMENT")
    
    class Config:
        env_prefix = "BROKER_"


class LoggingConfig(BaseSettings):
    """Logging configuration."""
    
    LOG_LEVEL: str = Field(default="INFO", env="LOG_LEVEL")
    LOG_DIR: str = Field(default="logs", env="LOG_DIR")
    LOG_JSON: bool = Field(default=True, env="LOG_JSON")
    LOG_MAX_BYTES: int = Field(default=10485760, env="LOG_MAX_BYTES")  # 10MB
    LOG_BACKUP_COUNT: int = Field(default=5, env="LOG_BACKUP_COUNT")
    
    @validator("LOG_LEVEL")
    def validate_log_level(cls, v):
        """Validate log level."""
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if v.upper() not in valid_levels:
            raise ValueError(f"Invalid log level: {v}. Must be one of {valid_levels}")
        return v.upper()
    
    class Config:
        env_prefix = "LOG_"


class APIConfig(BaseSettings):
    """API configuration."""
    
    API_HOST: str = Field(default="0.0.0.0", env="API_HOST")
    API_PORT: int = Field(default=8000, env="API_PORT")
    API_WORKERS: int = Field(default=4, env="API_WORKERS")
    API_RELOAD: bool = Field(default=False, env="API_RELOAD")
    
    # CORS settings
    CORS_ORIGINS: list[str] = Field(default=["http://localhost:3000"], env="CORS_ORIGINS")
    CORS_ALLOW_CREDENTIALS: bool = Field(default=True, env="CORS_ALLOW_CREDENTIALS")
    CORS_ALLOW_METHODS: list[str] = Field(default=["*"], env="CORS_ALLOW_METHODS")
    CORS_ALLOW_HEADERS: list[str] = Field(default=["*"], env="CORS_ALLOW_HEADERS")
    
    # Compression
    ENABLE_COMPRESSION: bool = Field(default=True, env="ENABLE_COMPRESSION")
    COMPRESSION_LEVEL: int = Field(default=6, env="COMPRESSION_LEVEL")
    
    class Config:
        env_prefix = "API_"


class AppConfig(BaseSettings):
    """Application configuration."""
    
    ENVIRONMENT: Environment = Field(default=Environment.DEVELOPMENT, env="ENVIRONMENT")
    APP_NAME: str = Field(default="Automate Trading Platform", env="APP_NAME")
    APP_VERSION: str = Field(default="1.0.0", env="APP_VERSION")
    DEBUG: bool = Field(default=False, env="DEBUG")
    
    # Feature flags
    ENABLE_WEBSOCKET: bool = Field(default=True, env="ENABLE_WEBSOCKET")
    ENABLE_BACKTEST: bool = Field(default=True, env="ENABLE_BACKTEST")
    ENABLE_PAPER_TRADING: bool = Field(default=True, env="ENABLE_PAPER_TRADING")
    ENABLE_LIVE_TRADING: bool = Field(default=False, env="ENABLE_LIVE_TRADING")
    
    @validator("DEBUG")
    def validate_debug(cls, v, values):
        """Debug should only be True in development."""
        env = values.get("ENVIRONMENT", Environment.DEVELOPMENT)
        if env != Environment.DEVELOPMENT and v:
            raise ValueError("Debug mode should only be enabled in development")
        return v
    
    @validator("ENABLE_LIVE_TRADING")
    def validate_live_trading(cls, v, values):
        """Live trading should only be enabled in production with proper setup."""
        env = values.get("ENVIRONMENT", Environment.DEVELOPMENT)
        if env != Environment.PRODUCTION and v:
            raise ValueError("Live trading should only be enabled in production")
        return v
    
    class Config:
        env_prefix = "APP_"


class Settings:
    """Main settings class that aggregates all configuration."""
    
    def __init__(self):
        self.database = DatabaseConfig()
        self.redis = RedisConfig()
        self.security = SecurityConfig()
        self.broker = BrokerConfig()
        self.logging = LoggingConfig()
        self.api = APIConfig()
        self.app = AppConfig()
    
    @property
    def is_production(self) -> bool:
        """Check if running in production."""
        return self.app.ENVIRONMENT == Environment.PRODUCTION
    
    @property
    def is_development(self) -> bool:
        """Check if running in development."""
        return self.app.ENVIRONMENT == Environment.DEVELOPMENT
    
    @property
    def is_staging(self) -> bool:
        """Check if running in staging."""
        return self.app.ENVIRONMENT == Environment.STAGING
    
    def validate(self) -> list[str]:
        """Validate all configuration settings."""
        errors = []
        
        # Validate required secrets in production
        if self.is_production:
            if self.security.JWT_SECRET_KEY == "change-this-in-production":
                errors.append("JWT_SECRET_KEY must be changed in production")
            if self.security.CSRF_SECRET == "change-this-in-production":
                errors.append("CSRF_SECRET must be changed in production")
            if not self.broker.BROKER_API_KEY:
                errors.append("BROKER_API_KEY is required in production")
            if not self.broker.BROKER_API_SECRET:
                errors.append("BROKER_API_SECRET is required in production")
        
        return errors


# Global settings instance
settings = Settings()


def get_settings() -> Settings:
    """Get the global settings instance."""
    return settings
