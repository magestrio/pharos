// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title IStrategyAdapter
/// @notice Adapter contract used by Vault8004 multi-call execution layer.
///
///         Base asset on Mantle: WETH = 0xdEAddEaDdeadDEadDEADDEAddEADDEAddead1111.
///         All `valueInBaseAsset()` returns MUST be denominated in WETH wei (18 decimals).
///
///         Oracle rules every implementation MUST follow:
///           1. The chosen oracle MUST be manipulation-resistant. Instantaneous
///              pool-liquidity prices are forbidden: do NOT call
///              `LBRouter.getSwapOut()`, `Pair.getReserves()`, or any analog as
///              a price source. Allowed: Chainlink-style feeds, Aave Oracle,
///              or TWAPs with a minimum observation window.
///           2. Liveness — if the oracle source exposes a timestamp,
///              `valueInBaseAsset()` MUST revert on stale data
///              (heartbeat exceeded). Where no on-chain timestamp is exposed
///              (e.g. some L2 Aave Oracle proxies on Mantle), the
///              implementation MUST document the fallback: typically the
///              vault-level Chainlink L2 Sequencer Uptime Feed
///              (`Vault8004.sequencerUptimeFeed`) plus the source protocol's
///              own economic incentive to maintain feed liveness.
///           3. `valueInBaseAsset()` MUST revert on `answer <= 0`. It MAY
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
    ///      (e.g. aToken balance for Aave adapters, sUSDe units for Ethena).
    ///      NOT priced — different adapters return non-comparable numbers.
    ///      Vault accounting MUST use `valueInBaseAsset()` instead.
    function balance() external view returns (uint256);

    /// @notice Address of the token this adapter accepts as input/output.
    function asset() external view returns (address);

    /// @notice Current value of the adapter's position priced in the vault's
    ///         base asset (WETH wei, 18 decimals).
    /// @dev Must follow the oracle rules in the interface-level NatSpec.
    ///
    ///      Reference oracle choices per known adapter family:
    ///
    ///        - AaveV3WethAdapter:
    ///            Position is WETH-denominated (aWETH 1:1 with WETH).
    ///            No external oracle required; return aToken balance directly.
    ///
    ///        - AaveV3UsdcAdapter:
    ///            Price USDC -> WETH via Aave Oracle on Mantle
    ///            (0x47a063CfDa980532267970d478EC340C0F80E8df), using
    ///            `getAssetPrice(USDC)` and `getAssetPrice(WETH)` (both
    ///            USD, 1e8). The Aave Oracle is the only verified on-chain
    ///            price source on Mantle Mainnet at the time of this
    ///            interface (2026-05-23).
    ///
    ///        - AaveV3SusdeAdapter:
    ///            Two-leg pricing. (a) sUSDe -> USDe via a TWAP source with
    ///            min observation window — Merchant Moe LB sUSDe/USDe pair
    ///            (0xE50019C79Cbd7C49cfFA7C3f8080EA238DE75962) or Curve
    ///            sUSDe/USDe pool (verify deployment before relying on it).
    ///            (b) USDe -> WETH via Aave Oracle.
    ///            Implementations MUST document the chosen TWAP window and
    ///            reject if observations are insufficient.
    ///
    ///      Any new adapter MUST document its oracle source in the
    ///      implementation NatSpec or be blocked at review.
    function valueInBaseAsset() external view returns (uint256);
}
