from enum import Enum

from pydantic import Field
from pydantic_settings import BaseSettings


class DatabaseType(str, Enum):
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"
    MYSQL = "mysql"


class Settings(BaseSettings):
    app_name: str = "ATE Cloud API"
    debug: bool = False
    nats_url: str = "nats://localhost:4222"

    # Database configuration
    database_type: DatabaseType = Field(default=DatabaseType.SQLITE)
    database_url: str = ""  # Auto-constructed if empty
    db_pool_size: int = Field(default=5, ge=1, le=20)
    db_max_overflow: int = Field(default=10, ge=0)

    # SQLite specific
    sqlite_db_path: str = "data/ate_platform.db"

    # PostgreSQL specific
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_user: str = "postgres"
    pg_password: str = "postgres"
    pg_database: str = "ate_platform"

    # MySQL specific
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = "root"
    mysql_database: str = "ate_platform"

    # Qdrant configuration
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection_failures: str = "ate_failures"
    embedding_dimensions: int = 1536  # DeepAgents / OpenAI compatible

    # Upload queue settings
    upload_queue_max_size: int = Field(default=1000, ge=1, description="Maximum number of entries in upload queue before pruning")
    upload_queue_max_age_seconds: int = Field(default=3600, ge=1, description="Maximum age in seconds before upload queue entries are pruned")

    class Config:
        env_prefix = "ATE_CLOUD_"

    def get_database_url(self) -> str:
        """Construct database URL based on database_type."""
        if self.database_url:
            return self.database_url

        if self.database_type == DatabaseType.SQLITE:
            return f"sqlite+aiosqlite:///{self.sqlite_db_path}"
        elif self.database_type == DatabaseType.POSTGRESQL:
            return f"postgresql+asyncpg://{self.pg_user}:{self.pg_password}@{self.pg_host}:{self.pg_port}/{self.pg_database}"
        elif self.database_type == DatabaseType.MYSQL:
            return f"mysql+aiomysql://{self.mysql_user}:{self.mysql_password}@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
        else:
            raise ValueError(f"Unsupported database type: {self.database_type}")


settings = Settings()
