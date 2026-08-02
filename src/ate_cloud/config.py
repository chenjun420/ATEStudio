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

    # Simulation mode — when True, all drivers are created in SIM mode
    # (no PyVISA connections, simulated instrument responses)
    simulation_mode: bool = Field(
        default=False,
        validation_alias="ATE_SIMULATION_MODE",
        description="Enable global simulation mode for all instrument drivers",
    )

    # OpenAI / LLM configuration (no ATE_CLOUD_ prefix)
    openai_api_key: str = Field(
        default="",
        validation_alias="OPENAI_API_KEY",
        description="API key for LLM / embedding services (OpenAI or compatible)",
    )
    openai_base_url: str = Field(
        default="",
        validation_alias="OPENAI_BASE_URL",
        description="Base URL for OpenAI-compatible API (e.g. Aliyun DashScope). Empty = OpenAI default.",
    )
    openai_model: str = Field(
        default="gpt-4o-mini",
        validation_alias="OPENAI_MODEL",
        description="Chat model name for LLM features (e.g. gpt-4o-mini, qwen-plus)",
    )
    openai_embedding_model: str = Field(
        default="text-embedding-3-small",
        validation_alias="OPENAI_EMBEDDING_MODEL",
        description="Embedding model name (e.g. text-embedding-3-small, qwen3.7-text-embedding)",
    )

    # Neo4j configuration (no ATE_CLOUD_ prefix)
    neo4j_url: str = Field(
        default="bolt://localhost:7687",
        validation_alias="NEO4J_URL",
        description="Neo4j Bolt connection URL",
    )
    neo4j_password: str = Field(
        default="atestudio",
        validation_alias="NEO4J_PASSWORD",
        description="Neo4j database password",
    )

    # JWT authentication configuration (no ATE_CLOUD_ prefix)
    jwt_secret: str = Field(
        default="",
        validation_alias="JWT_SECRET",
        description="JWT signing secret key",
    )
    jwt_algorithm: str = Field(
        default="RS256",
        validation_alias="JWT_ALGORITHM",
        description="JWT signing algorithm",
    )
    jwt_expire_minutes: int = Field(
        default=30,
        validation_alias="JWT_EXPIRE_MINUTES",
        description="JWT token expiration in minutes",
    )

    # Development mode (no ATE_CLOUD_ prefix - uses ATE_DEV_MODE directly)
    dev_mode: bool = Field(
        default=False,
        validation_alias="ATE_DEV_MODE",
        description="Enable development mode (extended debug, relaxed checks)",
    )

    # AI diagnosis auto-push (no ATE_CLOUD_ prefix - uses ATE_AI_DIAGNOSE_AUTO directly)
    ai_diagnose_auto: bool = Field(
        default=False,
        validation_alias="ATE_AI_DIAGNOSE_AUTO",
        description="Enable automatic push of AI diagnosis results to operator UI via NATS",
    )

    class Config:
        env_prefix = "ATE_CLOUD_"
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

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
