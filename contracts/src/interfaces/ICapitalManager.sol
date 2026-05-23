// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

/// @title ICapitalManager
/// @notice Minimal surface of `CapitalManager` consumed by `VUSDC`.
///         Kept narrow on purpose — vUSDC must never depend on
///         agent/owner/whitelist functions.
interface ICapitalManager {
    /// @notice Base asset (USDC, 6 decimals).
    function usdc() external view returns (IERC20);

    /// @notice Total assets under management, denominated in USDC (6 decimals).
    ///         Sum of free USDC balance + every whitelisted adapter's
    ///         `valueInUsdc()`. Reverts on stale-oracle / sequencer-down per
    ///         CapitalManager fail-loud semantics; the revert propagates into
    ///         `exchangeRate()` and blocks user-facing mints/redeems.
    function totalAssetsUsdc() external view returns (uint256);

    /// @notice Pull `usdcAmount` USDC from the caller (vUSDC) into the manager.
    ///         Caller MUST hold the USDC and approve `usdcAmount` to this
    ///         contract beforehand. `onlyVusdc`, `nonReentrant`, `whenNotPaused`.
    function recordDeposit(uint256 usdcAmount) external;

    /// @notice Transfer `usdcAmount` USDC from the manager to `to`. Called by
    ///         vUSDC on user redemption. `onlyVusdc`, `nonReentrant`,
    ///         `whenNotPaused`. Reverts if free USDC balance is insufficient.
    function recordWithdraw(uint256 usdcAmount, address to) external;
}
