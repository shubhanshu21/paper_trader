"""
utils/redis_cache.py — Redis caching layer for performance optimization.

Provides production-grade caching with TTL, serialization, and
cache invalidation strategies.
"""
import json
import pickle
from typing import Any, Optional, Callable
from functools import wraps
import redis

from automate.config.environment import get_settings


class RedisCache:
    """Redis cache manager with serialization and TTL support."""
    
    def __init__(self):
        settings = get_settings()
        redis_config = settings.redis
        
        self.redis_client = redis.from_url(
            redis_config.redis_url,
            max_connections=redis_config.REDIS_MAX_CONNECTIONS,
            decode_responses=False,  # Handle binary data for pickle
            socket_timeout=5,
            socket_connect_timeout=5,
        )
        
        self.default_ttl = 3600  # 1 hour default
    
    def _serialize(self, value: Any) -> bytes:
        """Serialize value for storage."""
        try:
            # Try JSON first for text data
            return json.dumps(value).encode('utf-8')
        except (TypeError, ValueError):
            # Fall back to pickle for complex objects
            return pickle.dumps(value)
    
    def _deserialize(self, value: bytes) -> Any:
        """Deserialize value from storage."""
        try:
            # Try JSON first
            return json.loads(value.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Fall back to pickle
            return pickle.loads(value)
    
    def get(self, key: str) -> Optional[Any]:
        """
        Get value from cache.
        
        Args:
            key: Cache key
            
        Returns:
            Cached value or None if not found
        """
        try:
            value = self.redis_client.get(key)
            if value is None:
                return None
            return self._deserialize(value)
        except Exception as e:
            # Log error but don't fail the application
            print(f"Redis get error: {e}")
            return None
    
    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """
        Set value in cache.
        
        Args:
            key: Cache key
            value: Value to cache
            ttl: Time to live in seconds (uses default if not specified)
            
        Returns:
            True if successful, False otherwise
        """
        try:
            serialized = self._serialize(value)
            expire = ttl or self.default_ttl
            return self.redis_client.setex(key, expire, serialized)
        except Exception as e:
            print(f"Redis set error: {e}")
            return False
    
    def delete(self, key: str) -> bool:
        """
        Delete value from cache.
        
        Args:
            key: Cache key
            
        Returns:
            True if successful, False otherwise
        """
        try:
            return bool(self.redis_client.delete(key))
        except Exception as e:
            print(f"Redis delete error: {e}")
            return False
    
    def delete_pattern(self, pattern: str) -> int:
        """
        Delete keys matching a pattern.
        
        Args:
            pattern: Redis key pattern
            
        Returns:
            Number of keys deleted
        """
        try:
            keys = self.redis_client.keys(pattern)
            if keys:
                return self.redis_client.delete(*keys)
            return 0
        except Exception as e:
            print(f"Redis delete_pattern error: {e}")
            return 0
    
    def exists(self, key: str) -> bool:
        """
        Check if key exists in cache.
        
        Args:
            key: Cache key
            
        Returns:
            True if key exists, False otherwise
        """
        try:
            return bool(self.redis_client.exists(key))
        except Exception as e:
            print(f"Redis exists error: {e}")
            return False
    
    def clear(self) -> bool:
        """
        Clear all cache entries.
        
        Returns:
            True if successful, False otherwise
        """
        try:
            return self.redis_client.flushdb()
        except Exception as e:
            print(f"Redis clear error: {e}")
            return False
    
    def get_many(self, keys: list) -> dict:
        """
        Get multiple values from cache.
        
        Args:
            keys: List of cache keys
            
        Returns:
            Dictionary of key-value pairs
        """
        result = {}
        try:
            values = self.redis_client.mget(keys)
            for key, value in zip(keys, values):
                if value is not None:
                    result[key] = self._deserialize(value)
        except Exception as e:
            print(f"Redis get_many error: {e}")
        return result
    
    def set_many(self, mapping: dict, ttl: Optional[int] = None) -> bool:
        """
        Set multiple values in cache.
        
        Args:
            mapping: Dictionary of key-value pairs
            ttl: Time to live in seconds
            
        Returns:
            True if successful, False otherwise
        """
        try:
            pipe = self.redis_client.pipeline()
            expire = ttl or self.default_ttl
            for key, value in mapping.items():
                serialized = self._serialize(value)
                pipe.setex(key, expire, serialized)
            pipe.execute()
            return True
        except Exception as e:
            print(f"Redis set_many error: {e}")
            return False


def cached(ttl: int = 3600, key_prefix: str = ""):
    """
    Decorator for caching function results.
    
    Args:
        ttl: Time to live in seconds
        key_prefix: Prefix for cache keys
        
    Usage:
        @cached(ttl=600, key_prefix="user:")
        def get_user(user_id):
            return db.query(User).get(user_id)
    """
    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            cache = RedisCache()
            
            # Generate cache key
            key_parts = [key_prefix, func.__name__]
            key_parts.extend(str(arg) for arg in args)
            key_parts.extend(f"{k}={v}" for k, v in sorted(kwargs.items()))
            cache_key = ":".join(key_parts)
            
            # Try to get from cache
            cached_value = cache.get(cache_key)
            if cached_value is not None:
                return cached_value
            
            # Execute function and cache result
            result = func(*args, **kwargs)
            cache.set(cache_key, result, ttl)
            
            return result
        return wrapper
    return decorator


def invalidate_cache_pattern(pattern: str) -> bool:
    """
    Invalidate cache entries matching a pattern.
    
    Args:
        pattern: Redis key pattern to match
        
    Returns:
        True if successful, False otherwise
    """
    cache = RedisCache()
    return cache.delete_pattern(pattern) > 0


# Global cache instance
cache = RedisCache()


def get_cache() -> RedisCache:
    """Get the global cache instance."""
    return cache
