"""
utils/input_sanitizer.py — Input sanitization and validation.

Provides production-grade input sanitization to prevent injection attacks
and ensure data integrity.
"""
import re
import html
from typing import Any, Optional, List, Dict
from urllib.parse import urlparse


class InputSanitizer:
    """Input sanitization utilities."""
    
    # SQL injection patterns
    SQL_INJECTION_PATTERNS = [
        r"(\b(SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|EXEC|UNION)\b)",
        r"(--|;|\/\*|\*\/)",
        r"(\bOR\b.*=.*\bOR\b)",
        r"(\bAND\b.*=.*\bAND\b)",
        r"(\bWHERE\b.*=.*\bOR\b)",
    ]
    
    # XSS patterns
    XSS_PATTERNS = [
        r"<script[^>]*>.*?</script>",
        r"javascript:",
        r"on\w+\s*=",
        r"<iframe[^>]*>",
        r"<object[^>]*>",
        r"<embed[^>]*>",
    ]
    
    @classmethod
    def sanitize_string(cls, input_str: str, max_length: int = 1000) -> str:
        """
        Sanitize string input.
        
        Args:
            input_str: Input string to sanitize
            max_length: Maximum allowed length
            
        Returns:
            Sanitized string
        """
        if not isinstance(input_str, str):
            return str(input_str)
        
        # Truncate to max length
        sanitized = input_str[:max_length]
        
        # Remove potential SQL injection patterns
        for pattern in cls.SQL_INJECTION_PATTERNS:
            sanitized = re.sub(pattern, "", sanitized, flags=re.IGNORECASE)
        
        # Remove potential XSS patterns
        for pattern in cls.XSS_PATTERNS:
            sanitized = re.sub(pattern, "", sanitized, flags=re.IGNORECASE)
        
        # HTML escape
        sanitized = html.escape(sanitized)
        
        # Strip whitespace
        sanitized = sanitized.strip()
        
        return sanitized
    
    @classmethod
    def sanitize_number(cls, input_val: Any, min_val: Optional[float] = None, max_val: Optional[float] = None) -> Optional[float]:
        """
        Sanitize numeric input.
        
        Args:
            input_val: Input value to sanitize
            min_val: Minimum allowed value
            max_val: Maximum allowed value
            
        Returns:
            Sanitized number or None if invalid
        """
        try:
            num = float(input_val)
            
            if min_val is not None and num < min_val:
                return min_val
            if max_val is not None and num > max_val:
                return max_val
                
            return num
        except (ValueError, TypeError):
            return None
    
    @classmethod
    def sanitize_integer(cls, input_val: Any, min_val: Optional[int] = None, max_val: Optional[int] = None) -> Optional[int]:
        """
        Sanitize integer input.
        
        Args:
            input_val: Input value to sanitize
            min_val: Minimum allowed value
            max_val: Maximum allowed value
            
        Returns:
            Sanitized integer or None if invalid
        """
        try:
            num = int(input_val)
            
            if min_val is not None and num < min_val:
                return min_val
            if max_val is not None and num > max_val:
                return max_val
                
            return num
        except (ValueError, TypeError):
            return None
    
    @classmethod
    def sanitize_email(cls, email: str) -> Optional[str]:
        """
        Sanitize and validate email address.
        
        Args:
            email: Email address to sanitize
            
        Returns:
            Sanitized email or None if invalid
        """
        if not isinstance(email, str):
            return None
        
        email = email.strip().lower()
        
        # Basic email validation
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        
        if not re.match(email_pattern, email):
            return None
        
        return email
    
    @classmethod
    def sanitize_url(cls, url: str, allowed_schemes: List[str] = None) -> Optional[str]:
        """
        Sanitize and validate URL.
        
        Args:
            url: URL to sanitize
            allowed_schemes: List of allowed URL schemes
            
        Returns:
            Sanitized URL or None if invalid
        """
        if not isinstance(url, str):
            return None
        
        url = url.strip()
        
        try:
            parsed = urlparse(url)
            
            if allowed_schemes and parsed.scheme not in allowed_schemes:
                return None
            
            return url
        except Exception:
            return None
    
    @classmethod
    def sanitize_list(cls, input_list: Any, item_type: type = str, max_items: int = 100) -> List:
        """
        Sanitize list input.
        
        Args:
            input_list: Input list to sanitize
            item_type: Expected type of list items
            max_items: Maximum number of items allowed
            
        Returns:
            Sanitized list
        """
        if not isinstance(input_list, (list, tuple)):
            return []
        
        sanitized = []
        
        for item in input_list[:max_items]:
            if item_type == str:
                sanitized.append(cls.sanitize_string(item))
            elif item_type == int:
                sanitized.append(cls.sanitize_integer(item))
            elif item_type == float:
                sanitized.append(cls.sanitize_number(item))
            else:
                sanitized.append(item)
        
        return sanitized
    
    @classmethod
    def sanitize_dict(cls, input_dict: Any, allowed_keys: Optional[List[str]] = None) -> Dict:
        """
        Sanitize dictionary input.
        
        Args:
            input_dict: Input dictionary to sanitize
            allowed_keys: List of allowed keys (None = all keys allowed)
            
        Returns:
            Sanitized dictionary
        """
        if not isinstance(input_dict, dict):
            return {}
        
        sanitized = {}
        
        for key, value in input_dict.items():
            # Check if key is allowed
            if allowed_keys and key not in allowed_keys:
                continue
            
            # Sanitize key
            sanitized_key = cls.sanitize_string(str(key))
            
            # Sanitize value based on type
            if isinstance(value, str):
                sanitized[sanitized_key] = cls.sanitize_string(value)
            elif isinstance(value, (int, float)):
                sanitized[sanitized_key] = value
            elif isinstance(value, list):
                sanitized[sanitized_key] = cls.sanitize_list(value)
            elif isinstance(value, dict):
                sanitized[sanitized_key] = cls.sanitize_dict(value)
            else:
                sanitized[sanitized_key] = value
        
        return sanitized
    
    @classmethod
    def validate_symbol(cls, symbol: str) -> bool:
        """
        Validate trading symbol format.
        
        Args:
            symbol: Trading symbol to validate
            
        Returns:
            True if valid, False otherwise
        """
        if not isinstance(symbol, str):
            return False
        
        # Common Indian stock/index symbol pattern
        pattern = r'^[A-Z]{1,20}$'
        
        return bool(re.match(pattern, symbol.strip().upper()))
    
    @classmethod
    def validate_phone(cls, phone: str) -> bool:
        """
        Validate phone number format.
        
        Args:
            phone: Phone number to validate
            
        Returns:
            True if valid, False otherwise
        """
        if not isinstance(phone, str):
            return False
        
        # Indian phone number pattern (10 digits)
        pattern = r'^[6-9]\d{9}$'
        
        return bool(re.match(pattern, phone.strip()))


def sanitize_input(input_data: Any, input_type: str = "string", **kwargs) -> Any:
    """
    Generic input sanitization function.
    
    Args:
        input_data: Input data to sanitize
        input_type: Type of input (string, number, integer, email, url, list, dict)
        **kwargs: Additional arguments for specific sanitizers
        
    Returns:
        Sanitized data
    """
    sanitizer = InputSanitizer()
    
    if input_type == "string":
        return sanitizer.sanitize_string(input_data, kwargs.get("max_length", 1000))
    elif input_type == "number":
        return sanitizer.sanitize_number(input_data, kwargs.get("min_val"), kwargs.get("max_val"))
    elif input_type == "integer":
        return sanitizer.sanitize_integer(input_data, kwargs.get("min_val"), kwargs.get("max_val"))
    elif input_type == "email":
        return sanitizer.sanitize_email(input_data)
    elif input_type == "url":
        return sanitizer.sanitize_url(input_data, kwargs.get("allowed_schemes"))
    elif input_type == "list":
        return sanitizer.sanitize_list(input_data, kwargs.get("item_type", str), kwargs.get("max_items", 100))
    elif input_type == "dict":
        return sanitizer.sanitize_dict(input_data, kwargs.get("allowed_keys"))
    else:
        return input_data
