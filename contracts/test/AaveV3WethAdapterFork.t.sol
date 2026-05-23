// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {AaveV3WethAdapter} from "../src/adapters/AaveV3WethAdapter.sol";

/// @notice Fork-test for AaveV3WethAdapter against live Mantle mainnet Aave V3.
/// Run:  MANTLE_RPC_URL=https://rpc.mantle.xyz forge test --match-contract AaveV3WethAdapterFork -vv
/// or:   forge test --match-contract AaveV3WethAdapterFork -vv  (uses default fallback URL)
contract AaveV3WethAdapterForkTest is Test {
    // Mantle Mainnet — Aave V3 (verified via bgd-labs/aave-address-book)
    address constant AAVE_POOL = 0x458F293454fE0d67EC0655f3672301301DD51422;
    address constant WETH      = 0xdEAddEaDdeadDEadDEADDEAddEADDEAddead1111;
    address constant aWETH     = 0xeAC30Ed8609F564aE65C809C4bf42dB2fF426D2C;

    AaveV3WethAdapter adapter;
    address vault = address(this);
    address owner = address(0xBEEF);

    function setUp() public {
        string memory rpc = vm.envOr("MANTLE_RPC_URL", string("https://rpc.mantle.xyz"));
        vm.createSelectFork(rpc);

        adapter = new AaveV3WethAdapter(AAVE_POOL, WETH, aWETH, vault, owner);
    }

    function test_Deposit_SuppliesToAave_LiveMantle() public {
        uint256 amount = 1e18; // 1 WETH

        deal(WETH, vault, amount);
        IERC20(WETH).approve(address(adapter), amount);

        uint256 aBalBefore = IERC20(aWETH).balanceOf(address(adapter));
        adapter.deposit(amount);
        uint256 aBalAfter = IERC20(aWETH).balanceOf(address(adapter));

        // Aave aToken minting can round down by 1 wei vs supply amount due to liquidity index math.
        assertApproxEqAbs(aBalAfter - aBalBefore, amount, 1, "aWETH delta != deposit");
        assertApproxEqAbs(adapter.balance(),       amount, 1, "adapter.balance() != deposit");
        assertEq(IERC20(WETH).balanceOf(vault), 0, "WETH not pulled from vault");
    }

    function test_Withdraw_ReturnsToVault_LiveMantle() public {
        uint256 amount = 1e18;

        deal(WETH, vault, amount);
        IERC20(WETH).approve(address(adapter), amount);
        adapter.deposit(amount);

        // Withdraw exactly what the adapter holds in aTokens (vault tracks balance, not deposit amount).
        // Aave rounds aToken minting down, so deposit(amount) may leave balance = amount - 1.
        uint256 toWithdraw = adapter.balance();
        uint256 wethBefore = IERC20(WETH).balanceOf(vault);
        adapter.withdraw(toWithdraw);
        uint256 wethAfter = IERC20(WETH).balanceOf(vault);

        assertApproxEqAbs(wethAfter - wethBefore, amount, 1, "WETH not returned to vault");
        assertLe(adapter.balance(), 1, "adapter still holds aWETH dust > 1 wei");
    }
}
