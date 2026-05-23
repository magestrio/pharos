// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @notice Subset of Aave V3 oracle used by adapters to price positions in WETH.
interface IAaveOracle {
    /// @notice Returns asset price in BASE_CURRENCY (USD for the Mantle deployment),
    ///         scaled to BASE_CURRENCY_UNIT (1e8 on Mantle).
    function getAssetPrice(address asset) external view returns (uint256);

    /// @notice Returns the Chainlink-style aggregator that backs `asset`. Used by
    ///         adapters to perform a fresh staleness check on the underlying feed.
    function getSourceOfAsset(address asset) external view returns (address);
}

/// @notice Chainlink V2 aggregator minimal surface used by Aave V3 Mantle feeds.
/// @dev Mantle's Aave price source proxies expose only `latestAnswer()`. Neither
///      `latestRoundData()` (V3) nor `latestTimestamp()` (V2 timestamp) is
///      available, so on-chain heartbeat checks are not possible from the
///      adapter; liveness is enforced via vault-level sequencer-uptime feed
///      (see `CapitalManager._checkSequencer`) and Aave's own economic security.
interface IChainlinkAggregator {
    function latestAnswer() external view returns (int256);
}
