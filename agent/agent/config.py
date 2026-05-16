from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    rpc_url: str = "https://rpc.mantle.xyz"
    private_key: str = ""
    anthropic_api_key: str = ""
    pinata_api_key: str = ""
    pinata_secret_key: str = ""
    vault_address: str = "0x0000000000000000000000000000000000000000"
    decision_log_address: str = "0x0000000000000000000000000000000000000000"

    model_config = {"env_file": "../.env", "env_file_encoding": "utf-8"}


settings = Settings()
