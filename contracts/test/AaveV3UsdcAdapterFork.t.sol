// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {AaveV3UsdcAdapter} from "../src/adapters/AaveV3UsdcAdapter.sol";

/// @notice Fork-test for AaveV3UsdcAdapter against live Mantle mainnet Aave V3.
/// Run:  MANTLE_RPC_URL=https://rpc.mantle.xyz forge test --match-contract AaveV3UsdcAdapterFork -vv
/// or:   forge test --match-contract AaveV3UsdcAdapterFork -vv  (uses default fallback URL)
contract AaveV3UsdcAdapterForkTest is Test {
    // Mantle Mainnet — Aave V3 (verified via bgd-labs/aave-address-book)
    address constant AAVE_POOL = 0x458F293454fE0d67EC0655f3672301301DD51422;
    address constant USDC      = 0x09Bc4E0D864854c6aFB6eB9A9cdF58aC190D0dF9;
    address constant aUSDC     = 0xcb8164415274515867ec43CbD284ab5d6d2b304F;

    AaveV3UsdcAdapter adapter;
    address vault = address(this); // test contract acts as vault
    address owner = address(0xBEEF);

    function setUp() public {
        string memory rpc = vm.envOr("MANTLE_RPC_URL", string("https://rpc.mantle.xyz"));
        vm.createSelectFork(rpc);

        adapter = new AaveV3UsdcAdapter(AAVE_POOL, USDC, aUSDC, vault, owner);
    }

    function test_Deposit_SuppliesToAave_LiveMantle() public {
        uint256 amount = 1_000e6; // 1000 USDC

        deal(USDC, vault, amount);
        IERC20(USDC).approve(address(adapter), amount);

        uint256 aBalBefore = IERC20(aUSDC).balanceOf(address(adapter));
        adapter.deposit(amount);
        uint256 aBalAfter = IERC20(aUSDC).balanceOf(address(adapter));

        // Aave aToken minting can round down by 1 wei vs supply amount due to liquidity index math.
        assertApproxEqAbs(aBalAfter - aBalBefore, amount, 1, "aUSDC delta != deposit");
        assertApproxEqAbs(adapter.balance(),       amount, 1, "adapter.balance() != deposit");
        assertEq(IERC20(USDC).balanceOf(vault), 0, "USDC not pulled from vault");
    }

    function test_Withdraw_ReturnsToVault_LiveMantle() public {
        uint256 amount = 1_000e6;

        // setup: deposit
        deal(USDC, vault, amount);
        IERC20(USDC).approve(address(adapter), amount);
        adapter.deposit(amount);

        // Withdraw exactly what the adapter holds in aTokens (vault tracks balance, not deposit amount).
        // Aave rounds aToken minting down, so deposit(amount) may leave balance = amount - 1.
        uint256 toWithdraw = adapter.balance();
        uint256 usdcBefore = IERC20(USDC).balanceOf(vault);
        adapter.withdraw(toWithdraw);
        uint256 usdcAfter = IERC20(USDC).balanceOf(vault);

        assertApproxEqAbs(usdcAfter - usdcBefore, amount, 1, "USDC not returned to vault");
        assertLe(adapter.balance(), 1, "adapter still holds aUSDC dust > 1 wei");
    }

    function test_ValueInUsdc_TracksAUsdcBalance_LiveMantle() public {
        uint256 amount = 1_000e6; // 1000 USDC

        deal(USDC, vault, amount);
        IERC20(USDC).approve(address(adapter), amount);
        adapter.deposit(amount);

        // aUSDC is 1:1 with USDC. valueInUsdc should equal aUSDC.balanceOf directly,
        // which is the deposited amount (up to 1 wei rounding).
        assertEq(adapter.valueInUsdc(), IERC20(aUSDC).balanceOf(address(adapter)), "valueInUsdc != aUSDC balance");
        assertApproxEqAbs(adapter.valueInUsdc(), amount, 1, "valueInUsdc drifted from deposit");
    }

    function test_ValueInUsdc_ZeroOnEmpty_LiveMantle() public view {
        assertEq(adapter.valueInUsdc(), 0, "empty adapter should value 0");
    }
}
