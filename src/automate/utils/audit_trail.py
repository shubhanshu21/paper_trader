"""
utils/audit_trail.py — Audit trail logging for compliance and security.

Provides comprehensive audit logging for all critical operations
including user actions, data changes, and system events.
"""
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from enum import Enum
from sqlalchemy import text
import json

from automate.db.engine import get_db
from automate.config.environment import get_settings


class AuditAction(str, Enum):
    """Audit action types."""
    CREATE = "CREATE"
    READ = "READ"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    LOGIN = "LOGIN"
    LOGOUT = "LOGOUT"
    DEPLOY = "DEPLOY"
    TRADE = "TRADE"
    BACKTEST = "BACKTEST"
    CONFIG_CHANGE = "CONFIG_CHANGE"
    ERROR = "ERROR"
    SYSTEM = "SYSTEM"


class AuditLogger:
    """Audit trail logger for compliance and security."""
    
    def __init__(self):
        self.logger = logging.getLogger("audit")
        self.settings = get_settings()
    
    def log(
        self,
        action: AuditAction,
        user_id: Optional[int],
        resource_type: str,
        resource_id: Optional[str],
        details: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        success: bool = True,
        error_message: Optional[str] = None
    ) -> bool:
        """
        Log an audit event.
        
        Args:
            action: Type of action performed
            user_id: ID of the user performing the action
            resource_type: Type of resource affected
            resource_id: ID of the specific resource
            details: Additional details about the action
            ip_address: IP address of the request
            user_agent: User agent string
            success: Whether the action was successful
            error_message: Error message if action failed
            
        Returns:
            True if logged successfully, False otherwise
        """
        try:
            audit_data = {
                "timestamp": datetime.utcnow().isoformat(),
                "action": action.value,
                "user_id": user_id,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "details": details or {},
                "ip_address": ip_address,
                "user_agent": user_agent,
                "success": success,
                "error_message": error_message,
                "environment": self.settings.app.ENVIRONMENT.value
            }
            
            # Log to structured logger
            self.logger.info(json.dumps(audit_data))
            
            # Store in database for persistence
            if self.settings.is_production:
                self._store_in_database(audit_data)
            
            return True
        except Exception as e:
            self.logger.error(f"Failed to log audit event: {e}")
            return False
    
    def _store_in_database(self, audit_data: Dict[str, Any]) -> bool:
        """
        Store audit event in database.
        
        Args:
            audit_data: Audit event data
            
        Returns:
            True if stored successfully, False otherwise
        """
        try:
            with get_db() as db:
                # Create audit_trail table if it doesn't exist
                db.execute(text("""
                    CREATE TABLE IF NOT EXISTS audit_trail (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        timestamp DATETIME NOT NULL,
                        action VARCHAR(32) NOT NULL,
                        user_id BIGINT,
                        resource_type VARCHAR(64) NOT NULL,
                        resource_id VARCHAR(255),
                        details JSON,
                        ip_address VARCHAR(45),
                        user_agent TEXT,
                        success BOOLEAN DEFAULT TRUE,
                        error_message TEXT,
                        environment VARCHAR(32),
                        INDEX idx_timestamp (timestamp),
                        INDEX idx_user_id (user_id),
                        INDEX idx_action (action),
                        INDEX idx_resource (resource_type, resource_id)
                    )
                """))
                
                # Insert audit record
                db.execute(text("""
                    INSERT INTO audit_trail (
                        timestamp, action, user_id, resource_type, resource_id,
                        details, ip_address, user_agent, success, error_message, environment
                    ) VALUES (
                        :timestamp, :action, :user_id, :resource_type, :resource_id,
                        :details, :ip_address, :user_agent, :success, :error_message, :environment
                    )
                """), {
                    "timestamp": audit_data["timestamp"],
                    "action": audit_data["action"],
                    "user_id": audit_data["user_id"],
                    "resource_type": audit_data["resource_type"],
                    "resource_id": audit_data["resource_id"],
                    "details": json.dumps(audit_data["details"]),
                    "ip_address": audit_data["ip_address"],
                    "user_agent": audit_data["user_agent"],
                    "success": audit_data["success"],
                    "error_message": audit_data["error_message"],
                    "environment": audit_data["environment"]
                })
                
                db.commit()
                return True
        except Exception as e:
            self.logger.error(f"Failed to store audit in database: {e}")
            return False
    
    def get_user_activity(
        self,
        user_id: int,
        limit: int = 100,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """
        Get audit trail for a specific user.
        
        Args:
            user_id: User ID to query
            limit: Maximum number of records
            offset: Offset for pagination
            
        Returns:
            List of audit records
        """
        try:
            with get_db() as db:
                result = db.execute(text("""
                    SELECT * FROM audit_trail
                    WHERE user_id = :user_id
                    ORDER BY timestamp DESC
                    LIMIT :limit OFFSET :offset
                """), {"user_id": user_id, "limit": limit, "offset": offset})
                
                return [dict(row._mapping) for row in result]
        except Exception as e:
            self.logger.error(f"Failed to get user activity: {e}")
            return []
    
    def get_resource_history(
        self,
        resource_type: str,
        resource_id: str,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Get audit trail for a specific resource.
        
        Args:
            resource_type: Type of resource
            resource_id: ID of the resource
            limit: Maximum number of records
            
        Returns:
            List of audit records
        """
        try:
            with get_db() as db:
                result = db.execute(text("""
                    SELECT * FROM audit_trail
                    WHERE resource_type = :resource_type
                    AND resource_id = :resource_id
                    ORDER BY timestamp DESC
                    LIMIT :limit
                """), {"resource_type": resource_type, "resource_id": resource_id, "limit": limit})
                
                return [dict(row._mapping) for row in result]
        except Exception as e:
            self.logger.error(f"Failed to get resource history: {e}")
            return []
    
    def get_failed_logins(self, hours: int = 24) -> List[Dict[str, Any]]:
        """
        Get failed login attempts for security monitoring.
        
        Args:
            hours: Number of hours to look back
            
        Returns:
            List of failed login attempts
        """
        try:
            with get_db() as db:
                result = db.execute(text("""
                    SELECT * FROM audit_trail
                    WHERE action = 'LOGIN'
                    AND success = FALSE
                    AND timestamp > DATE_SUB(NOW(), INTERVAL :hour HOUR)
                    ORDER BY timestamp DESC
                """), {"hour": hours})
                
                return [dict(row._mapping) for row in result]
        except Exception as e:
            self.logger.error(f"Failed to get failed logins: {e}")
            return []


def audit_log(
    action: AuditAction,
    resource_type: str,
    resource_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    user_id: Optional[int] = None,
    **kwargs
) -> bool:
    """
    Convenience function for audit logging.
    
    Args:
        action: Type of action
        resource_type: Type of resource
        resource_id: ID of resource
        details: Additional details
        user_id: User ID
        **kwargs: Additional audit parameters
        
    Returns:
        True if logged successfully
    """
    auditor = AuditLogger()
    return auditor.log(
        action=action,
        user_id=user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details,
        **kwargs
    )


# Global audit logger instance
audit_logger = AuditLogger()


def get_audit_logger() -> AuditLogger:
    """Get the global audit logger instance."""
    return audit_logger
