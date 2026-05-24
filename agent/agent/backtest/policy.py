from typing import Protocol
from agent.gather.models import MarketData
from agent.reason.schema import LegacyTargetAllocation


class PolicyProtocol(Protocol):
    name: str

    def decide(self, market: MarketData, current_alloc: dict[str, float]) -> LegacyTargetAllocation: ...


class DummyPolicy:
    """Static: 40% mETH / 20% cmETH / 25% sUSDe / 10% Aave / 5% cash."""
    name = "Dummy_Static"

    def decide(self, market: MarketData, current_alloc: dict[str, float]) -> LegacyTargetAllocation:
        return LegacyTargetAllocation(
            mETH_staked=0.40,
            cmETH=0.20,
            sUSDe=0.25,
            lendle_usdc=0.10,  # lendle_usdc field = Aave USDC position (Week 2 rename)
            cash=0.05,
        )


class HumanPMPolicy:
    """60% mETH / 30% sUSDe / 10% Aave USDC."""
    name = "Human_PM"

    def decide(self, market: MarketData, current_alloc: dict[str, float]) -> LegacyTargetAllocation:
        return LegacyTargetAllocation(
            mETH_staked=0.57,  # adjusted down to satisfy cash >= 0.03 constraint
            cmETH=0.0,
            sUSDe=0.30,
            lendle_usdc=0.10,  # lendle_usdc field = Aave USDC position (Week 2 rename)
            cash=0.03,
        )
