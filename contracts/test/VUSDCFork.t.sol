// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {CapitalManager} from "../src/CapitalManager.sol";
import {VUSDC} from "../src/VUSDC.sol";
import {ICapitalManager} from "../src/interfaces/ICapitalManager.sol";

/// @notice Fork test for VUSDC on Mantle mainnet against a real CapitalManager
///         and real USDC (no mocks).
///
///         Yield is simulated by directly `deal`-ing extra USDC into the
///         CapitalManager — bypasses the agent allocation path entirely so
///         the test isolates vUSDC accounting from adapter logic (covered by
///         CapitalManagerEndToEndFork.t.sol).
///
/// Run: forge test --match-contract VUSDCForkTest -vv
contract VUSDCForkTest is Test {
    // Mantle Mainnet — verified address.
    address constant USDC = 0x09Bc4E0D864854c6aFB6eB9A9cdF58aC190D0dF9;

    CapitalManager cm;
    VUSDC          vusdc;

    address owner = address(this);
    address alice = address(0xA11CE);

    function setUp() public {
        string memory rpc = vm.envOr("MANTLE_RPC_URL", string("https://rpc.mantle.xyz"));
        vm.createSelectFork(rpc);

        // No sequencer feed (address(0)) — disables liveness check, fine for
        // a pure vUSDC accounting test that doesn't touch adapters.
        cm    = new CapitalManager(IERC20(USDC), owner, address(0));
        vusdc = new VUSDC(ICapitalManager(address(cm)));
        cm.setVusdc(address(vusdc));

        deal(USDC, alice, 1_000e6);
        vm.prank(alice);
        IERC20(USDC).approve(address(vusdc), type(uint256).max);
    }

    /// @notice Full lifecycle per epic spec:
    ///   mint $100 → check vUSDC balance →
    ///   simulate yield in CapitalManager → check exchange rate grows →
    ///   redeem half → check USDC returned.
    function test_FullCycle_MintYieldRedeem_LiveMantle() public {
        // ── Mint $100 ────────────────────────────────────────────────────────
        vm.prank(alice);
        uint256 minted = vusdc.mint(100e6, alice);
        assertEq(minted, 100e6, "first mint 1:1");
        assertEq(vusdc.balanceOf(alice), 100e6, "vUSDC credited");
        assertEq(IERC20(USDC).balanceOf(address(cm)), 100e6, "CM holds USDC");
        assertEq(vusdc.exchangeRate(), 1e18, "rate at 1.0 post-first-mint");

        // ── Simulate yield: bump CM USDC balance by $50 ──────────────────────
        deal(USDC, address(cm), 150e6);
        assertEq(cm.totalAssetsUsdc(), 150e6, "CM ta after yield");
        assertEq(vusdc.exchangeRate(), 1.5e18, "rate at 1.5 post-yield");

        // ── Redeem half (50 vUSDC → 75 USDC at rate 1.5) ─────────────────────
        uint256 usdcBefore = IERC20(USDC).balanceOf(alice);
        vm.prank(alice);
        uint256 returned = vusdc.redeem(50e6, alice);
        assertEq(returned, 75e6, "redeem returns 50*150/100");
        assertEq(IERC20(USDC).balanceOf(alice), usdcBefore + 75e6, "alice received USDC");
        assertEq(vusdc.balanceOf(alice), 50e6, "alice keeps 50 vUSDC");

        // Post-state sanity: ta=75, supply=50, rate still 1.5e18.
        assertEq(cm.totalAssetsUsdc(), 75e6, "CM ta after redeem");
        assertEq(vusdc.totalSupply(), 50e6, "supply after redeem");
        assertEq(vusdc.exchangeRate(), 1.5e18, "rate stable post-redeem");
    }

    /// @notice Sanity: second user mints AFTER yield and gets proportionally
    /// fewer shares — proves rate-aware pricing on real CM, not just mock.
    function test_SecondUserMintsAtHigherRate_LiveMantle() public {
        address bob = address(0xB0B);
        deal(USDC, bob, 1_000e6);
        vm.prank(bob);
        IERC20(USDC).approve(address(vusdc), type(uint256).max);

        // Alice mints, then yield 2x.
        vm.prank(alice);
        vusdc.mint(100e6, alice);
        deal(USDC, address(cm), 200e6);

        // Bob mints 100 USDC at rate 2.0 → expects 50 vUSDC (100 * 100 / 200).
        vm.prank(bob);
        uint256 minted = vusdc.mint(100e6, bob);
        assertEq(minted, 50e6, "bob gets 50 vUSDC at rate 2");
        assertEq(vusdc.totalSupply(), 150e6, "supply = 100 + 50");
        // ta = 200 (yield-bumped) + 100 (bob's actual deposit) = 300, supply = 150 → rate still 2.0
        assertEq(vusdc.exchangeRate(), 2e18, "rate preserved across mint");
    }
}
