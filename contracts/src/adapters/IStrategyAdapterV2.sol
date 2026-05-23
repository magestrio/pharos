// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title IStrategyAdapterV2
/// @notice v2 adapter contract used by Vault8004v2 multi-call execution layer.
/// @dev Differs from v1 by adding `valueInBaseAsset()` so the vault can
///      honestly price heterogeneous positions across whitelisted adapters
///      in a single base unit (WETH wei, 18 decimals).
///
///      Base asset on Mantle: WETH = 0xdEAddEaDdeadDEadDEADDEAddEADDEAddead1111.
///      All `valueInBaseAsset()` returns MUST be denominated in WETH wei.
///
///      Oracle rules every implementation MUST follow:
///        1. The chosen oracle MUST be manipulation-resistant. Instantaneous
///           pool-liquidity prices are forbidden: do NOT call
///           `LBRouter.getSwapOut()`, `Pair.getReserves()`, or any analog as
///           a price source. Allowed: Chainlink-style feeds, Aave Oracle,
///           or TWAPs with a minimum observation window.
///        2. `valueInBaseAsset()` MUST revert if its oracle data is stale
///           (heartbeat exceeded or sequencer-uptime feed reports downtime).
///           Returning a possibly-wrong value is worse than reverting; the
///           vault's `totalAssets()` and ERC-4626 share price depend on it.
///        3. `valueInBaseAsset()` MAY return 0 only when the underlying
///           position is genuinely 0. It MUST NOT return 0 to mask oracle
///           failures.
///        4. The chosen oracle source MUST be documented in the
///           implementation's NatSpec, including the feed address(es) and
///           the staleness threshold used.
interface IStrategyAdapterV2 {
    /// @notice Deposit `amount` of `asset()` into the underlying strategy.
    function deposit(uint256 amount) external;

    /// @notice Withdraw up to `amount` from the strategy back to the vault.
    /// @return actualWithdrawn assets actually returned to the vault (may differ
    ///         from `amount` due to protocol-side rounding or balance shortfalls).
    function withdraw(uint256 amount) external returns (uint256 actualWithdrawn);

    /// @notice Address of the token this adapter accepts as input/output.
    /// @dev For native-WETH strategies this returns WETH. For non-WETH
    ///      strategies (e.g. USDC, sUSDe) this returns the strategy's native
    ///      token and the vault is expected to route via swap-adapter calls
    ///      before/after deposit/withdraw.
    function asset() external view returns (address);

    /// @notice Raw position size in the adapter's native unit.
    /// @dev Returned in the unit native to the underlying protocol
    ///      (e.g. aToken balance for Aave adapters, sUSDe units for Ethena).
    ///      NOT priced — different adapters return non-comparable numbers.
    ///      Kept on the interface for adapter-internal logic and off-chain
    ///      debugging. Vault accounting MUST use `valueInBaseAsset()` instead.
    function balance() external view returns (uint256);

    /// @notice Current value of the adapter's position priced in the vault's
    ///         base asset (WETH wei, 18 decimals).
    /// @dev Must follow the oracle rules in the interface-level NatSpec.
    ///
    ///      Reference oracle choices per known adapter family:
    ///
    ///        - AaveV3WethAdapter (v2):
    ///            Position is already WETH-denominated (aWETH 1:1 with WETH).
    ///            No external oracle required; return aToken balance directly.
    ///
    ///        - AaveV3UsdcAdapter (v2):
    ///            Price USDC -> WETH via Aave Oracle on Mantle
    ///            (0x47a063CfDa980532267970d478EC340C0F80E8df), using
    ///            `getAssetPrice(USDC)` and `getAssetPrice(WETH)` (both
    ///            USD, 1e8). The Aave Oracle is the only verified on-chain
    ///            price source on Mantle Mainnet at the time of this
    ///            interface (2026-05-23).
    ///
    ///        - AaveV3SusdeAdapter (v2):
    ///            Two-leg pricing. (a) sUSDe -> USDe via a TWAP source with
    ///            min observation window — Merchant Moe LB sUSDe/USDe pair
    ///            (0xE50019C79Cbd7C49cfFA7C3f8080EA238DE75962) or Curve
    ///            sUSDe/USDe pool (verify deployment in subtask .4 before
    ///            relying on it). (b) USDe -> WETH via Aave Oracle.
    ///            Implementations MUST document the chosen TWAP window and
    ///            reject if observations are insufficient.
    ///
    ///      Any new adapter MUST document its oracle source in the
    ///      implementation NatSpec or be blocked at review.
    function valueInBaseAsset() external view returns (uint256);
}
