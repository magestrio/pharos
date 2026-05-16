from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    MANTLE_RPC_URL: str = "https://rpc.mantle.xyz"
    MANTLE_SEPOLIA_RPC_URL: str = "https://rpc.sepolia.mantle.xyz"
    VAULT_ADDRESS: str = "0x0000000000000000000000000000000000000000"
    AGENT_PRIVATE_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    ALLORA_API_KEY: str = ""
    PINATA_JWT: str = ""


settings = Settings()
