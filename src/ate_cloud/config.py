from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "ATE Cloud API"
    debug: bool = False
    nats_url: str = "nats://localhost:4222"

    class Config:
        env_prefix = "ATE_CLOUD_"


settings = Settings()
