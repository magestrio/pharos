from pydantic import BaseModel, Field


class DepositRequested(BaseModel):
    """Mirror of `BybitAttestor.DepositRequested(uint256 txId, uint256 amount)`."""

    tx_id: int = Field(ge=0)
    amount: int = Field(ge=0)  # USDC, 6 decimals
    tx_hash: str
    log_index: int
    block_number: int


class WithdrawRequested(BaseModel):
    """Mirror of `BybitAttestor.WithdrawRequested(uint256 txId, uint256 amount)`."""

    tx_id: int = Field(ge=0)
    amount: int = Field(gt=0)  # withdraw of 0 is rejected on-chain
    tx_hash: str
    log_index: int
    block_number: int
