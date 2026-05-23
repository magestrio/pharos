// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

interface IStrategyAdapter {
    function deposit(uint256 amount) external;

    /// @notice Withdraw up to `amount` from the strategy back to the vault.
    /// @return actualWithdrawn assets actually returned to the vault (may differ
    ///         from `amount` due to protocol-side rounding or balance shortfalls).
    function withdraw(uint256 amount) external returns (uint256 actualWithdrawn);

    function balance() external view returns (uint256);
    function asset() external view returns (address);
}
