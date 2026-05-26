// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

import {VUSDC} from "../src/VUSDC.sol";
import {ICapitalManager} from "../src/interfaces/ICapitalManager.sol";

// ─────────────────────────────────────────────────────────────────────────────
// Mocks
// ─────────────────────────────────────────────────────────────────────────────

contract MockUSDC is ERC20 {
    constructor() ERC20("Mock USDC", "mUSDC") {}
    function decimals() public pure override returns (uint8) { return 6; }
    function mint(address to, uint256 amount) external { _mint(to, amount); }
}

/// @notice Minimal CapitalManager substitute for VUSDC unit tests.
///         Holds USDC; `totalAssetsUsdc()` is simply its USDC balance, so yield
///         is simulated by minting more mUSDC directly into this contract.
///         No access control — vUSDC is the only thing that calls it in tests.
contract MockCapitalManager is ICapitalManager {
    using SafeERC20 for IERC20;

    IERC20 public immutable _usdc;
    /// @notice Synthetic "off-chain / in-adapter" assets — counted toward
    ///         `totalAssetsUsdc()` but not held as actual USDC balance.
    ///         Lets tests simulate capital deployed to adapters and trigger
    ///         the `redeem` insufficient-onchain-liquidity path.
    uint256 public offchainAssets;

    constructor(IERC20 u) { _usdc = u; }

    function setOffchainAssets(uint256 v) external { offchainAssets = v; }

    function usdc() external view returns (IERC20) { return _usdc; }

    function totalAssetsUsdc() external view returns (uint256) {
        return _usdc.balanceOf(address(this)) + offchainAssets;
    }

    function recordDeposit(uint256 amount) external {
        _usdc.safeTransferFrom(msg.sender, address(this), amount);
    }

    function recordWithdraw(uint256 amount, address to) external {
        _usdc.safeTransfer(to, amount);
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Test suite
// ─────────────────────────────────────────────────────────────────────────────

contract VUSDCTest is Test {
    MockUSDC usdc;
    MockCapitalManager cm;
    VUSDC vusdc;

    address alice = address(0xA11CE);
    address bob   = address(0xB0B);

    event Minted(address indexed payer, address indexed to, uint256 usdcIn, uint256 vusdcOut);
    event Redeemed(address indexed burner, address indexed to, uint256 vusdcIn, uint256 usdcOut);

    function setUp() public {
        usdc  = new MockUSDC();
        cm    = new MockCapitalManager(IERC20(address(usdc)));
        vusdc = new VUSDC(ICapitalManager(address(cm)));

        // Seed both users with 1M USDC and pre-approve vUSDC.
        usdc.mint(alice, 1_000_000e6);
        usdc.mint(bob,   1_000_000e6);
        vm.prank(alice); usdc.approve(address(vusdc), type(uint256).max);
        vm.prank(bob);   usdc.approve(address(vusdc), type(uint256).max);
    }

    // ── Helpers ──────────────────────────────────────────────────────────────

    function _mint(address who, uint256 usdcAmt) internal returns (uint256) {
        vm.prank(who);
        return vusdc.mint(usdcAmt, who);
    }

    function _redeem(address who, uint256 vusdcAmt) internal returns (uint256) {
        vm.prank(who);
        return vusdc.redeem(vusdcAmt, who);
    }

    /// @dev "Add yield" = mint synthetic USDC directly into the CM so
    ///      totalAssetsUsdc grows without a corresponding share mint.
    function _addYield(uint256 amount) internal {
        usdc.mint(address(cm), amount);
    }

    // ── Constructor & metadata ───────────────────────────────────────────────

    function test_constructor_setsCapitalManager() public view {
        assertEq(address(vusdc.capitalManager()), address(cm));
    }

    function test_constructor_revertsOnZeroCapitalManager() public {
        vm.expectRevert(bytes("zero cm"));
        new VUSDC(ICapitalManager(address(0)));
    }

    function test_metadata() public view {
        assertEq(vusdc.name(),     "Vault USDC");
        assertEq(vusdc.symbol(),   "vUSDC");
        assertEq(vusdc.decimals(), 6);
    }

    // ── First mint & initial rate ────────────────────────────────────────────

    function test_exchangeRate_zeroSupplyReturns1e18() public view {
        assertEq(vusdc.exchangeRate(), 1e18);
    }

    function test_firstMint_isOneToOne() public {
        uint256 minted = _mint(alice, 100e6);
        assertEq(minted, 100e6);
        assertEq(vusdc.balanceOf(alice), 100e6);
        assertEq(usdc.balanceOf(address(cm)), 100e6);
    }

    function test_firstMint_keepsExchangeRateAt1e18() public {
        _mint(alice, 100e6);
        // No yield added → rate unchanged.
        assertEq(vusdc.exchangeRate(), 1e18);
    }

    // ── Yield accrual ────────────────────────────────────────────────────────

    function test_exchangeRate_growsWithTotalAssets() public {
        _mint(alice, 100e6);          // supply=100, ta=100, rate=1e18
        _addYield(50e6);              // ta=150, supply=100, rate=1.5e18
        assertEq(vusdc.exchangeRate(), 1.5e18);
    }

    function test_totalAssetsUsdc_aliasesExchangeRate() public {
        // Zero supply branch.
        assertEq(vusdc.totalAssetsUsdc(), vusdc.exchangeRate());
        // After mint + yield.
        _mint(alice, 100e6);
        _addYield(50e6);
        assertEq(vusdc.totalAssetsUsdc(), vusdc.exchangeRate());
        assertEq(vusdc.totalAssetsUsdc(), 1.5e18);
    }

    function test_mint_atHigherRate_givesFewerShares() public {
        _mint(alice, 100e6);          // supply=100, ta=100
        _addYield(100e6);             // ta=200 → rate=2.0
        uint256 minted = _mint(bob, 100e6); // shares = 100*100/200 = 50
        assertEq(minted, 50e6);
        assertEq(vusdc.balanceOf(bob), 50e6);
    }

    function test_redeem_atHigherRate_returnsMoreUsdc() public {
        _mint(alice, 100e6);          // alice has 100 vUSDC
        _addYield(100e6);             // ta=200 → each share worth 2 USDC
        uint256 returned = _redeem(alice, 50e6); // 50 * 200 / 100 = 100
        assertEq(returned, 100e6);
        assertEq(usdc.balanceOf(alice), 1_000_000e6 - 100e6 + 100e6); // back to start
    }

    // ── Redeem mechanics ─────────────────────────────────────────────────────

    function test_redeem_returnsProportionalUsdc() public {
        _mint(alice, 1000e6);
        uint256 returned = _redeem(alice, 400e6);
        assertEq(returned, 400e6);
        assertEq(vusdc.balanceOf(alice), 600e6);
    }

    function test_redeem_burnsSharesFromCaller() public {
        _mint(alice, 1000e6);
        uint256 supplyBefore = vusdc.totalSupply();
        _redeem(alice, 300e6);
        assertEq(vusdc.totalSupply(), supplyBefore - 300e6);
    }

    function test_redeem_revertsOnInsufficientBalance() public {
        _mint(alice, 100e6);
        vm.prank(bob);
        vm.expectRevert(); // OZ ERC20InsufficientBalance custom error
        vusdc.redeem(50e6, bob);
    }

    function test_redeem_revertsOnInsufficientOnchainLiquidity() public {
        _mint(alice, 100e6);
        // Simulate 60 USDC deployed to adapters: move it out of CM balance
        // and re-account it as offchainAssets so ta stays at 100.
        vm.prank(address(cm));
        usdc.transfer(address(0xDEAD), 60e6); // CM balance = 40
        cm.setOffchainAssets(60e6);            // ta still = 40 + 60 = 100
        // alice redeems all 100 vUSDC → owed 100*100/100 = 100 USDC, CM has 40 → revert
        vm.prank(alice);
        vm.expectRevert(); // safeTransfer revert from MockUSDC ERC20InsufficientBalance
        vusdc.redeem(100e6, alice);
    }

    // ── Guards & reverts ─────────────────────────────────────────────────────

    function test_mint_revertsOnZeroAmount() public {
        vm.prank(alice);
        vm.expectRevert(bytes("zero amount"));
        vusdc.mint(0, alice);
    }

    function test_mint_revertsOnZeroTo() public {
        vm.prank(alice);
        vm.expectRevert(bytes("zero to"));
        vusdc.mint(100e6, address(0));
    }

    function test_mint_revertsWhenWouldMintZero() public {
        // After massive yield, ratio rounds down to zero for tiny deposits.
        _mint(alice, 1e6);             // supply=1e6, ta=1e6
        _addYield(1e18);               // ta huge, supply tiny → 1 USDC mints 0 shares
        vm.prank(bob);
        vm.expectRevert(bytes("zero mint"));
        vusdc.mint(1, bob);            // 1 wei USDC
    }

    function test_mint_revertsWhenTotalAssetsZero() public {
        // Path: mint shares, then bleed CM to 0 — supply>0 with ta=0.
        _mint(alice, 100e6);
        vm.prank(address(cm));
        usdc.transfer(address(0xDEAD), 100e6);
        vm.prank(bob);
        vm.expectRevert(bytes("zero assets"));
        vusdc.mint(50e6, bob);
    }

    function test_redeem_revertsOnZeroAmount() public {
        vm.prank(alice);
        vm.expectRevert(bytes("zero amount"));
        vusdc.redeem(0, alice);
    }

    function test_redeem_revertsOnZeroTo() public {
        _mint(alice, 100e6);
        vm.prank(alice);
        vm.expectRevert(bytes("zero to"));
        vusdc.redeem(10e6, address(0));
    }

    // ── Events ───────────────────────────────────────────────────────────────

    function test_mint_emitsMintedEvent() public {
        vm.expectEmit(true, true, false, true, address(vusdc));
        emit Minted(alice, alice, 100e6, 100e6);
        _mint(alice, 100e6);
    }

    function test_redeem_emitsRedeemedEvent() public {
        _mint(alice, 100e6);
        vm.expectEmit(true, true, false, true, address(vusdc));
        emit Redeemed(alice, alice, 40e6, 40e6);
        _redeem(alice, 40e6);
    }

    // ── Previews ─────────────────────────────────────────────────────────────

    function test_previewMint_matchesMint_atRate1() public view {
        assertEq(vusdc.previewMint(100e6), 100e6);
    }

    function test_previewMint_matchesMint_afterYield() public {
        _mint(alice, 100e6);
        _addYield(100e6); // rate=2
        uint256 expected = vusdc.previewMint(60e6); // 60*100/200 = 30
        assertEq(expected, 30e6);
        uint256 actual = _mint(bob, 60e6);
        assertEq(actual, expected);
    }

    function test_previewRedeem_matchesRedeem() public {
        _mint(alice, 100e6);
        _addYield(50e6); // rate=1.5
        uint256 expected = vusdc.previewRedeem(40e6); // 40*150/100 = 60
        assertEq(expected, 60e6);
        uint256 actual = _redeem(alice, 40e6);
        assertEq(actual, expected);
    }

    function test_previewRedeem_zeroSupplyReturnsZero() public view {
        assertEq(vusdc.previewRedeem(1000e6), 0);
    }

    // ── Round-trip / no rounding leakage ─────────────────────────────────────

    function test_mintRedeem_symmetricWithoutYield() public {
        uint256 inUsdc = 12_345_678; // weird amount → catch rounding asymmetry
        uint256 minted = _mint(alice, inUsdc);
        uint256 returned = _redeem(alice, minted);
        // Without yield, full round-trip MUST return exactly the original USDC.
        assertEq(returned, inUsdc);
    }

    function test_multipleUsers_noValueLeakage() public {
        _mint(alice, 1000e6);
        _mint(bob,   500e6);
        // Bob redeems half; alice's value share must be unchanged.
        uint256 supplyBefore = vusdc.totalSupply();
        uint256 taBefore = cm.totalAssetsUsdc();
        uint256 aliceShareValueBefore = (vusdc.balanceOf(alice) * taBefore) / supplyBefore;

        _redeem(bob, 250e6);

        uint256 supplyAfter = vusdc.totalSupply();
        uint256 taAfter = cm.totalAssetsUsdc();
        uint256 aliceShareValueAfter = (vusdc.balanceOf(alice) * taAfter) / supplyAfter;

        assertEq(aliceShareValueAfter, aliceShareValueBefore);
    }
}
