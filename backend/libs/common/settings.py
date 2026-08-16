from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    storage_root: str = "./data"
    max_upload_mb: int = 1024
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
