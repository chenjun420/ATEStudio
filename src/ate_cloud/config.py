from enum import Enum

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseType(str, Enum):
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"
    MYSQL = "mysql"


class Settings(BaseSettings):
    # Pydantic v2 settings config (replaces the deprecated class-based
    # ``Config``). Behavior is identical: ATE_CLOUD_ env prefix, .env file
    # (utf-8), case-insensitive env var matching (pydantic-settings default),
    # and extra env vars ignored. Fields declaring an explicit
    # ``validation_alias`` (e.g. OPENAI_API_KEY) keep reading that exact name.
    model_config = SettingsConfigDict(
        env_prefix="ATE_CLOUD_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "ATE Cloud API"
    debug: bool = False
    nats_url: str = "nats://localhost:4222"

    # JetStream file storage (RH-2, doc §5/§10.5): when True the server runs
    # with file-backed streams (config/nats-server.conf store_dir
    # /var/lib/nats/jetstream) so offline events survive restarts.
    nats_file_store_enabled: bool = Field(
        default=True,
        description="Enable JetStream FILE storage for event streams (TESTSTATION_EVENTS)",
    )

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
    #: Vector width. This is NOT a free choice: it must equal what the configured
    #: embedding model actually returns, and models differ. OpenAI's
    #: text-embedding-3-small is 1536; the Qwen text-embedding model this
    #: deployment uses is 1024 (measured, not assumed). A mismatch against an
    #: existing Qdrant collection is detected at startup — see
    #: ``FailureIndexer.ensure_collection`` — because the alternative is insert
    #: failures that are logged and never raised, i.e. fault history silently
    #: stops accumulating.
    embedding_dimensions: int = 1536  # DeepAgents / OpenAI compatible
    #: Whether to tokenize locally and send token ids instead of strings.
    #: Defaults off, and the default is the deliberate choice: the tokenizer
    #: would be OpenAI's, which is the wrong tokenizer for any non-OpenAI
    #: embedding model — it mis-splits long text into batches, and providers
    #: that do not use that tokenizer reject the payload outright. DashScope's
    #: OpenAI-compatible endpoint answers ``input must be an array of strings``.
    #: Turn on only for a real OpenAI endpoint, and accept that the chunking is
    #: then tuned to OpenAI's tokenizer.
    embedding_check_ctx_length: bool = False

    # Upload queue settings
    upload_queue_max_size: int = Field(
        default=1000,
        ge=1,
        description="Maximum number of entries in upload queue before pruning",
    )
    upload_queue_max_age_seconds: int = Field(
        default=3600,
        ge=1,
        description="Maximum age in seconds before upload queue entries are pruned",
    )

    # Recordings directory — where edge RecordingInterceptor JSONL sessions land
    # (T10 finalize convention: <recordings_dir>/<run_id>.jsonl; consumed by the
    # T37 execution diff endpoint).
    recordings_dir: str = Field(
        default="/var/log/test_platform/recordings",
        validation_alias="ATE_RECORDINGS_DIR",
        description="Directory containing per-run JSONL recording files",
    )

    # Built single-page app to serve at "/".
    #
    # This exists because the deploy script builds the frontend, records
    # `frontend_built: true` in its stamp, and then nothing ever serves the
    # result: `create_app` mounted only the API router, so every asset path
    # 404'd while the stamp claimed the UI was up. The stamp was the only
    # record of the build, and it recorded a build, not a reachable page.
    #
    # Relative by default and resolved against the process CWD, which the
    # systemd unit pins to the checkout (/opt/atestudio). The env var is the
    # escape hatch for a layout that puts dist elsewhere.
    frontend_dist_dir: str = Field(
        default="frontend/dist",
        validation_alias="ATE_FRONTEND_DIST_DIR",
        description="Directory holding the built SPA (index.html + assets/)",
    )

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

    # Knowledge-graph backend configuration removed.
    #
    # Two blocks lived here. FalkorDB (FALKORDB_URL / FALKORDB_GRAPH /
    # FALKORDB_PASSWORD) backed the ontology graph that phase 1 does without:
    # the DENSO/JST field study measured the RAG baseline at F1@20 = 0.267 *while
    # already using an FMEA knowledge graph as its data source*, with the full
    # graph method reaching 0.523 only on a single line, the gains coming from
    # algorithms rather than storage. Fault cases now live in normalised
    # relational tables and are retrieved through Qdrant.
    #
    # Neo4j (NEO4J_URL / NEO4J_PASSWORD) was already dead — kept only "for
    # rollback/reference" by a comment, with no reader anywhere in the codebase.
    # Dead configuration is not a safety net; it is a way to give a future
    # reader the impression that a second graph backend could be switched back
    # on by flipping one setting.

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
