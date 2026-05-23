// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {ICapitalManager} from "./interfaces/ICapitalManager.sol";

/// @title vUSDC — yield-bearing USDC wrapper
/// @notice ERC-20 wrapper around USDC whose exchange rate accrues yield from
///         `CapitalManager`. Mint USDC → receive vUSDC at the current rate;
///         burn vUSDC → receive proportionally more USDC as the rate grows.
///
///         Pattern follows sUSDe / cToken (NOT ERC-4626): vUSDC is a liquid
///         stablecoin-style wrapper, not a fund-share token.
///
///         Trust model: holders trust `CapitalManager.totalAssetsUsdc()` to
///         report honest valuations of underlying positions. vUSDC itself is
///         intentionally minimal — no owner, no admin, no pause. Immutable
///         after deploy.
///
///         Decimals: 6, matching USDC, so wallets/UIs render balances on the
///         same scale as the underlying. Exchange rate (added in vusdc-token.4)
///         is scaled to 1e18 for precision regardless of token decimals.
contract VUSDC is ERC20 {
    /// @notice Underlying capital pool. Immutable after construction; the
    ///         relationship is sealed on the CapitalManager side by its
    ///         one-shot `setVusdc()` post-deploy wiring.
    ICapitalManager public immutable capitalManager;

    constructor(ICapitalManager _capitalManager) ERC20("Vault USDC", "vUSDC") {
        require(address(_capitalManager) != address(0), "zero cm");
        capitalManager = _capitalManager;
    }

    /// @inheritdoc ERC20
    /// @dev Override to 6 to match USDC. Default ERC20 implementation returns 18.
    function decimals() public pure override returns (uint8) {
        return 6;
    }
}
