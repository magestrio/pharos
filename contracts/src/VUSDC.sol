// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
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
    using SafeERC20 for IERC20;

    /// @notice Underlying capital pool. Immutable after construction; the
    ///         relationship is sealed on the CapitalManager side by its
    ///         one-shot `setVusdc()` post-deploy wiring.
    ICapitalManager public immutable capitalManager;

    event Minted(address indexed payer, address indexed to, uint256 usdcIn, uint256 vusdcOut);
    event Redeemed(address indexed burner, address indexed to, uint256 vusdcIn, uint256 usdcOut);

    constructor(ICapitalManager _capitalManager) ERC20("Vault USDC", "vUSDC") {
        require(address(_capitalManager) != address(0), "zero cm");
        capitalManager = _capitalManager;
    }

    /// @inheritdoc ERC20
    /// @dev Override to 6 to match USDC. Default ERC20 implementation returns 18.
    function decimals() public pure override returns (uint8) {
        return 6;
    }

    /// @notice Current vUSDC → USDC exchange rate, scaled to 1e18.
    /// @dev `1 vUSDC == exchangeRate() / 1e18 USDC` (in raw 6-decimal units).
    ///      The 1e18 base is independent of the token decimals (6) — it gives
    ///      bps-level precision on tiny yield increments. Initial rate when no
    ///      shares have been minted is 1e18 (1.0).
    ///
    ///      Reverts if any underlying adapter's oracle is stale or the L2
    ///      sequencer is down (propagated from `CapitalManager.totalAssetsUsdc`).
    function exchangeRate() external view returns (uint256) {
        uint256 supply = totalSupply();
        if (supply == 0) return 1e18;
        return (capitalManager.totalAssetsUsdc() * 1e18) / supply;
    }

    /// @notice Returns how much vUSDC `mint(usdcAmount, ...)` would credit at
    ///         current state. Mirrors `mint`'s math exactly (same rounding).
    /// @dev View-only — no state change, no transfers. Does NOT enforce the
    ///      `vusdcMinted > 0` or `ta > 0` guards from `mint`: a frontend
    ///      probing the function should be able to see "0" and explain it to
    ///      the user, rather than get a revert string. The underlying
    ///      `totalAssetsUsdc()` call can still revert on stale oracle / down
    ///      sequencer — that propagates intentionally.
    function previewMint(uint256 usdcAmount) external view returns (uint256) {
        uint256 supply = totalSupply();
        if (supply == 0) return usdcAmount;
        uint256 ta = capitalManager.totalAssetsUsdc();
        if (ta == 0) return 0;
        return (usdcAmount * supply) / ta;
    }

    /// @notice Returns how much USDC `redeem(vusdcAmount, ...)` would return
    ///         at current state. Mirrors `redeem`'s math exactly.
    /// @dev View-only. `supply == 0` returns 0 (no shares to redeem against)
    ///      rather than dividing by zero.
    function previewRedeem(uint256 vusdcAmount) external view returns (uint256) {
        uint256 supply = totalSupply();
        if (supply == 0) return 0;
        return (vusdcAmount * capitalManager.totalAssetsUsdc()) / supply;
    }

    /// @notice Mint vUSDC by depositing `usdcAmount` USDC at the current
    ///         exchange rate. The vUSDC is credited to `to`; USDC is pulled
    ///         from `msg.sender` (the payer may differ from the recipient).
    /// @dev Computes shares as a direct `usdcAmount * supply / totalAssets`
    ///      ratio rather than going through `exchangeRate()` — saves a
    ///      division and avoids the intermediate rounding step. First mint
    ///      (supply == 0) is 1:1.
    ///
    ///      `totalAssetsUsdc()` is read BEFORE the deposit so the depositor
    ///      doesn't pay yield rate on their own incoming USDC.
    ///
    ///      Flow: pull USDC from msg.sender → approve CapitalManager →
    ///      `recordDeposit()` → mint shares to `to`.
    /// @return vusdcMinted Amount of vUSDC credited to `to`.
    function mint(uint256 usdcAmount, address to) external returns (uint256 vusdcMinted) {
        require(usdcAmount > 0, "zero amount");
        require(to != address(0), "zero to");

        uint256 supply = totalSupply();
        if (supply == 0) {
            vusdcMinted = usdcAmount;
        } else {
            uint256 ta = capitalManager.totalAssetsUsdc();
            // Pathological: supply > 0 but reported assets == 0 means every
            // previously-minted share is worthless. Reject rather than mint at
            // infinite rate, which would silently dilute the incoming deposit.
            require(ta > 0, "zero assets");
            vusdcMinted = (usdcAmount * supply) / ta;
        }
        // Floor-rounding to zero when ratio < 1 unit of vUSDC. Protects the
        // depositor from losing USDC for no shares.
        require(vusdcMinted > 0, "zero mint");

        IERC20 usdc = capitalManager.usdc();
        usdc.safeTransferFrom(msg.sender, address(this), usdcAmount);
        usdc.forceApprove(address(capitalManager), usdcAmount);
        capitalManager.recordDeposit(usdcAmount);

        _mint(to, vusdcMinted);
        emit Minted(msg.sender, to, usdcAmount, vusdcMinted);
    }

    /// @notice Redeem `vusdcAmount` vUSDC from `msg.sender` and receive the
    ///         proportional USDC at `to`.
    /// @dev Computes return as `vusdcAmount * totalAssets / supply` BEFORE
    ///      burning, so the redeemer is valued at the pre-burn exchange rate.
    ///      Symmetric to `mint`.
    ///
    ///      Reverts (via `CapitalManager.recordWithdraw → safeTransfer`) if
    ///      the manager's free USDC balance is below `usdcReturned` — i.e. too
    ///      much capital sits in adapters / off-chain sleeves. Off-chain
    ///      orchestration (agent rebalance) must free cash before this call
    ///      succeeds. There is no partial-fill path; redemption is all-or-revert.
    ///
    ///      CEI: burn first, external transfer last. `recordWithdraw` is
    ///      `nonReentrant` on the manager side, so the external call cannot
    ///      re-enter `redeem`.
    /// @return usdcReturned Amount of USDC transferred to `to`.
    function redeem(uint256 vusdcAmount, address to) external returns (uint256 usdcReturned) {
        require(vusdcAmount > 0, "zero amount");
        require(to != address(0), "zero to");

        uint256 supply = totalSupply();
        // supply > 0 holds because the redeemer must own >= vusdcAmount shares
        // (enforced by _burn). Reading totalAssets here values the burnt shares
        // at the current rate before the underlying USDC leaves the manager.
        uint256 ta = capitalManager.totalAssetsUsdc();
        usdcReturned = (vusdcAmount * ta) / supply;
        // Floor-rounding can produce zero on dust redemptions when rate < 1e-6;
        // reject rather than let the caller burn shares for nothing.
        require(usdcReturned > 0, "zero return");

        _burn(msg.sender, vusdcAmount);
        capitalManager.recordWithdraw(usdcReturned, to);
        emit Redeemed(msg.sender, to, vusdcAmount, usdcReturned);
    }
}
