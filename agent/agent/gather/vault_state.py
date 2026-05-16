from pydantic import BaseModel


class VaultState(BaseModel):
    total_assets: int = 0
    mETH_staked: float = 0.0
    cmETH: float = 0.0
    sUSDe: float = 0.0
    lendle_usdc: float = 0.0
    cash: float = 1.0


async def get_vault_state() -> VaultState:
    raise NotImplementedError
