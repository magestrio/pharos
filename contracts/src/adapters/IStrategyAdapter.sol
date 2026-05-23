// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title IStrategyAdapter
/// @notice Adapter contract used by CapitalManager multi-call execution layer.
///
///         Base asset on Mantle: USDC = 0x09Bc4E0D864854c6aFB6eB9A9cdF58aC190D0dF9.
///         All `valueInUsdc()` returns MUST be denominated in USDC units (6 decimals).
///
///         Oracle rules every implementation MUST follow:
///           1. The chosen oracle MUST be manipulation-resistant. Instantaneous
///              pool-liquidity prices are forbidden: do NOT call
///              `LBRouter.getSwapOut()`, `Pair.getReserves()`, or any analog as
///              a price source. Allowed: Chainlink-style feeds, Aave Oracle,
///              or TWAPs with a minimum observation window.
///           2. Liveness — if the oracle source exposes a timestamp,
///              `valueInUsdc()` MUST revert on stale data
///              (heartbeat exceeded). Where no on-chain timestamp is exposed
///              (e.g. some L2 Aave Oracle proxies on Mantle), the
///              implementation MUST document the fallback: typically the
///              vault-level Chainlink L2 Sequencer Uptime Feed
///              (`CapitalManager.sequencerUptimeFeed`) plus the source protocol's
///              own economic incentive to maintain feed liveness.
///           3. `valueInUsdc()` MUST revert on `answer <= 0`. It MAY
///              return 0 only when the underlying position is genuinely 0.
///              It MUST NOT return 0 to mask oracle failures.
///           4. The chosen oracle source MUST be documented in the
///              implementation's NatSpec, including feed address(es) and the
///              staleness check (or its documented absence).
interface IStrategyAdapter {
    /// @notice Deposit `amount` of `asset()` into the underlying strategy.
    function deposit(uint256 amount) external;

    /// @notice Withdraw up to `amount` from the strategy back to the vault.
    /// @return actualWithdrawn assets actually returned to the vault (may differ
    ///         from `amount` due to protocol-side rounding or balance shortfalls).
    function withdraw(uint256 amount) external returns (uint256 actualWithdrawn);

    /// @notice Raw position size in the adapter's native unit.
    /// @dev Returned in the unit native to the underlying protocol
    ///      (e.g. aToken balance for Aave adapters). NOT priced — different
    ///      adapters return non-comparable numbers. Vault accounting MUST use
    ///      `valueInUsdc()` instead.
    function balance() external view returns (uint256);

    /// @notice Address of the token this adapter accepts as input/output.
    function asset() external view returns (address);

    /// @notice Current value of the adapter's position priced in USDC units
    ///         (6 decimals).
    /// @dev Must follow the oracle rules in the interface-level NatSpec.
    ///
    ///      Reference oracle choices per known adapter family:
    ///
    ///        - AaveV3UsdcAdapter:
    ///            Position is USDC-denominated (aUSDC 1:1 with USDC).
    ///            No external oracle required; return aToken balance directly.
    ///
    ///        - AaveV3WethAdapter:
    ///            Position is WETH-denominated (aWETH 1:1 with WETH, 18 decimals).
    ///            Price WETH -> USDC via Aave Oracle on Mantle
    ///            (0x47a063CfDa980532267970d478EC340C0F80E8df), using
    ///            `getAssetPrice(WETH)` and `getAssetPrice(USDC)` (both
    ///            USD, 1e8). Formula:
    ///              valueInUsdc = (aWeth * wethUsdPrice * 1e6) / (usdcUsdPrice * 1e18).
    ///            The Aave Oracle is the only verified on-chain price source
    ///            on Mantle Mainnet at the time of this interface (2026-05-23).
    ///
    ///      Any new adapter MUST document its oracle source in the
    ///      implementation NatSpec or be blocked at review.
    function valueInUsdc() external view returns (uint256);
}
