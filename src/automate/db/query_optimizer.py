"""
db/query_optimizer.py — Database query optimization utilities.

Provides query optimization techniques including indexing hints,
query analysis, and performance monitoring.
"""
import logging
from typing import List, Dict, Any, Optional
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.engine import Engine
from contextlib import contextmanager
import time

log = logging.getLogger(__name__)


class QueryOptimizer:
    """Database query optimization utilities."""
    
    def __init__(self, engine: Engine):
        self.engine = engine
    
    def analyze_query(self, query: str) -> Dict[str, Any]:
        """
        Analyze a query using EXPLAIN.
        
        Args:
            query: SQL query to analyze
            
        Returns:
            Query execution plan and analysis
        """
        try:
            with self.engine.connect() as conn:
                explain_query = f"EXPLAIN {query}"
                result = conn.execute(text(explain_query))
                rows = result.fetchall()
                
                return {
                    "query": query,
                    "execution_plan": [dict(row._mapping) for row in rows],
                    "rows_analyzed": len(rows)
                }
        except Exception as e:
            log.error(f"Query analysis failed: {e}")
            return {"error": str(e)}
    
    def get_table_indexes(self, table_name: str) -> List[Dict[str, Any]]:
        """
        Get indexes for a specific table.
        
        Args:
            table_name: Name of the table
            
        Returns:
            List of index information
        """
        try:
            with self.engine.connect() as conn:
                query = text("""
                    SELECT 
                        INDEX_NAME as index_name,
                        COLUMN_NAME as column_name,
                        SEQ_IN_INDEX as seq_in_index,
                        INDEX_TYPE as index_type
                    FROM INFORMATION_SCHEMA.STATISTICS
                    WHERE TABLE_SCHEMA = DATABASE()
                    AND TABLE_NAME = :table_name
                    ORDER BY INDEX_NAME, SEQ_IN_INDEX
                """)
                result = conn.execute(query, {"table_name": table_name})
                return [dict(row._mapping) for row in result]
        except Exception as e:
            log.error(f"Failed to get indexes for {table_name}: {e}")
            return []
    
    def get_slow_queries(self, threshold_seconds: float = 1.0) -> List[Dict[str, Any]]:
        """
        Get slow queries from MySQL slow query log.
        
        Args:
            threshold_seconds: Minimum execution time threshold
            
        Returns:
            List of slow query information
        """
        try:
            with self.engine.connect() as conn:
                query = text("""
                    SELECT 
                        sql_text as query,
                        exec_count as execution_count,
                        avg_timer_wait/1000000000000 as avg_time_seconds,
                        sum_timer_wait/1000000000000 as total_time_seconds
                    FROM performance_schema.events_statements_summary_by_digest
                    WHERE avg_timer_wait/1000000000000 > :threshold
                    ORDER BY avg_timer_wait DESC
                    LIMIT 20
                """)
                result = conn.execute(query, {"threshold": threshold_seconds})
                return [dict(row._mapping) for row in result]
        except Exception as e:
            log.error(f"Failed to get slow queries: {e}")
            return []
    
    def get_table_stats(self, table_name: str) -> Dict[str, Any]:
        """
        Get table statistics for optimization.
        
        Args:
            table_name: Name of the table
            
        Returns:
            Table statistics
        """
        try:
            with self.engine.connect() as conn:
                query = text("""
                    SELECT 
                        TABLE_ROWS as row_count,
                        AVG_ROW_LENGTH as avg_row_length,
                        DATA_LENGTH as data_length,
                        INDEX_LENGTH as index_length,
                        UPDATE_TIME as last_update
                    FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_SCHEMA = DATABASE()
                    AND TABLE_NAME = :table_name
                """)
                result = conn.execute(query, {"table_name": table_name})
                row = result.fetchone()
                return dict(row._mapping) if row else {}
        except Exception as e:
            log.error(f"Failed to get table stats for {table_name}: {e}")
            return {}


@contextmanager
def query_timer(operation_name: str):
    """
    Context manager to time database queries.
    
    Args:
        operation_name: Name of the operation being timed
        
    Usage:
        with query_timer("user_lookup"):
            user = db.query(User).get(user_id)
    """
    start_time = time.time()
    try:
        yield
    finally:
        elapsed_time = time.time() - start_time
        if elapsed_time > 1.0:  # Log slow queries
            log.warning(f"Slow query detected: {operation_name} took {elapsed_time:.2f}s")
        else:
            log.debug(f"Query {operation_name} took {elapsed_time:.4f}s")


def optimize_query(session: Session, query) -> Any:
    """
    Apply common query optimizations.
    
    Args:
        session: Database session
        query: SQLAlchemy query object
        
    Returns:
        Optimized query
    """
    # Enable query caching
    query = query.options()
    
    # Add execution options
    query = query.execution_options(compiled_cache=None)
    
    return query


def suggest_indexes(session: Session, table_name: str) -> List[Dict[str, Any]]:
    """
    Suggest indexes based on query patterns.
    
    Args:
        session: Database session
        table_name: Name of the table
        
    Returns:
        List of suggested indexes
    """
    suggestions = []
    
    try:
        # Get columns that are frequently used in WHERE clauses
        with session.bind.connect() as conn:
            query = text("""
                SELECT 
                    COLUMN_NAME as column_name,
                    CARDINALITY as cardinality
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                AND TABLE_NAME = :table_name
                AND IS_NULLABLE = 'NO'
                ORDER BY CARDINALITY DESC
            """)
            result = conn.execute(query, {"table_name": table_name})
            columns = [dict(row._mapping) for row in result]
            
            # Suggest indexes for high-cardinality columns
            for col in columns[:5]:  # Top 5 columns
                if col['cardinality'] > 100:
                    suggestions.append({
                        "column": col['column_name'],
                        "reason": "High cardinality column used in filters",
                        "type": "INDEX"
                    })
    
    except Exception as e:
        log.error(f"Failed to suggest indexes for {table_name}: {e}")
    
    return suggestions


class QueryCache:
    """Simple query result caching."""
    
    def __init__(self, max_size: int = 100):
        self.cache: Dict[str, Any] = {}
        self.max_size = max_size
    
    def get(self, key: str) -> Optional[Any]:
        """Get cached query result."""
        return self.cache.get(key)
    
    def set(self, key: str, value: Any) -> None:
        """Cache query result."""
        if len(self.cache) >= self.max_size:
            # Remove oldest entry (simple FIFO)
            oldest_key = next(iter(self.cache))
            del self.cache[oldest_key]
        self.cache[key] = value
    
    def clear(self) -> None:
        """Clear all cached results."""
        self.cache.clear()


# Global query optimizer instance
_query_optimizer: Optional[QueryOptimizer] = None


def get_query_optimizer(engine: Engine) -> QueryOptimizer:
    """Get the global query optimizer instance."""
    global _query_optimizer
    if _query_optimizer is None:
        _query_optimizer = QueryOptimizer(engine)
    return _query_optimizer
