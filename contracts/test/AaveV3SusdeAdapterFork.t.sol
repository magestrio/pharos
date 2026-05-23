// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {AaveV3SusdeAdapter} from "../src/adapters/AaveV3SusdeAdapter.sol";

/// @notice Fork-test for AaveV3SusdeAdapter against live Mantle mainnet Aave V3.
/// Run: forge test --match-contract AaveV3SusdeAdapterFork -vv
contract AaveV3SusdeAdapterForkTest is Test {
    address constant AAVE_POOL   = 0x458F293454fE0d67EC0655f3672301301DD51422;
    address constant AAVE_ORACLE = 0x47a063CfDa980532267970d478EC340C0F80E8df;
    address constant sUSDe       = 0x211Cc4DD073734dA055fbF44a2b4667d5E5fE5d2;
    address constant asUSDe      = 0xaf972F332FF79bd32A6CB6B54f903eA0F9b16C2a;
    address constant WETH        = 0xdEAddEaDdeadDEadDEADDEAddEADDEAddead1111;

    AaveV3SusdeAdapter adapter;
    address vault = address(this);
    address owner = address(0xBEEF);

    function setUp() public {
        string memory rpc = vm.envOr("MANTLE_RPC_URL", string("https://rpc.mantle.xyz"));
        vm.createSelectFork(rpc);

        adapter = new AaveV3SusdeAdapter(AAVE_POOL, AAVE_ORACLE, sUSDe, asUSDe, WETH, vault, owner);
    }

    function test_Deposit_SuppliesToAave_LiveMantle() public {
        uint256 amount = 1_000e18; // 1000 sUSDe

        deal(sUSDe, vault, amount);
        IERC20(sUSDe).approve(address(adapter), amount);

        uint256 aBalBefore = IERC20(asUSDe).balanceOf(address(adapter));
        adapter.deposit(amount);
        uint256 aBalAfter = IERC20(asUSDe).balanceOf(address(adapter));

        assertApproxEqAbs(aBalAfter - aBalBefore, amount, 1, "asUSDe delta != deposit");
        assertApproxEqAbs(adapter.balance(),       amount, 1, "adapter.balance() != deposit");
        assertEq(IERC20(sUSDe).balanceOf(vault), 0, "sUSDe not pulled from vault");
    }

    function test_Withdraw_ReturnsToVault_LiveMantle() public {
        uint256 amount = 1_000e18;

        deal(sUSDe, vault, amount);
        IERC20(sUSDe).approve(address(adapter), amount);
        adapter.deposit(amount);

        uint256 toWithdraw = adapter.balance();
        uint256 sUsdeBefore = IERC20(sUSDe).balanceOf(vault);
        adapter.withdraw(toWithdraw);
        uint256 sUsdeAfter  = IERC20(sUSDe).balanceOf(vault);

        assertApproxEqAbs(sUsdeAfter - sUsdeBefore, amount, 1, "sUSDe not returned to vault");
        assertLe(adapter.balance(), 1, "adapter still holds asUSDe dust > 1 wei");
    }

    function test_ValueInBaseAsset_PricesViaAaveOracle_LiveMantle() public {
        uint256 amount = 1_000e18; // 1000 sUSDe

        deal(sUSDe, vault, amount);
        IERC20(sUSDe).approve(address(adapter), amount);
        adapter.deposit(amount);

        // 1000 sUSDe ~= $1100+ (sUSDe trades slightly above $1 due to accrued yield).
        // At ETH ~$2500 that's ~0.44 WETH. Broad band: 0.05 to 5 WETH.
        uint256 valueWeth = adapter.valueInBaseAsset();
        assertGt(valueWeth, 0.05 ether, "value implausibly low");
        assertLt(valueWeth, 5 ether,    "value implausibly high");
    }

    function test_ValueInBaseAsset_ZeroOnEmpty_LiveMantle() public view {
        assertEq(adapter.valueInBaseAsset(), 0, "empty adapter should value 0");
    }
}
