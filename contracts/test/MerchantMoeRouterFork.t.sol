// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {MerchantMoeRouter} from "../src/adapters/MerchantMoeRouter.sol";
import {ILBRouter} from "../src/adapters/interfaces/ILBRouter.sol";

interface ILBRouterFull {
    function getSwapOut(address pair, uint128 amountIn, bool swapForY)
        external view returns (uint128 amountInLeft, uint128 amountOut, uint128 fee);
}

/// @notice Fork-test for MerchantMoeRouter against live Mantle mainnet liquidity.
/// Run:  MANTLE_RPC_URL=https://rpc.mantle.xyz forge test --match-contract MerchantMoeRouterFork -vv
/// or:   forge test --match-contract MerchantMoeRouterFork -vv  (uses the default fallback URL)
contract MerchantMoeRouterForkTest is Test {
    // Mantle Mainnet
    address constant LB_ROUTER = 0x013e138EF6008ae5FDFDE29700e3f2Bc61d21E3a;
    address constant USDC      = 0x09Bc4E0D864854c6aFB6eB9A9cdF58aC190D0dF9;
    address constant USDE      = 0x5d3a1Ff2b6BAb83b63cd9AD0787074081a52ef34;
    address constant SUSDE     = 0x211Cc4DD073734dA055fbF44a2b4667d5E5fE5d2;

    address constant PAIR_USDE_USDC  = 0x7e78B65d0525339dF5F4aA22b82d9e97584Da8FC; // bs=1, X=USDe Y=USDC
    address constant PAIR_SUSDE_USDE = 0xE50019C79Cbd7C49cfFA7C3f8080EA238DE75962; // bs=5, X=sUSDe Y=USDe

    uint256 constant SLIPPAGE_BPS_CAP = 200; // 2%

    MerchantMoeRouter router;
    address owner = address(0xBEEF);

    function setUp() public {
        string memory rpc = vm.envOr("MANTLE_RPC_URL", string("https://rpc.mantle.xyz"));
        vm.createSelectFork(rpc);

        router = new MerchantMoeRouter(LB_ROUTER, USDC, USDE, SUSDE, owner);
    }

    // ─── Forward: USDC → sUSDe ───────────────────────────────────────────────

    function test_SwapToSusde_LiveMantle() public {
        uint256 amountIn = 1000e6; // 1000 USDC

        // Expected output via chained getSwapOut: USDC→USDe (swapForY=false), then USDe→sUSDe (swapForY=false)
        ( , uint128 usdeOut, ) = ILBRouterFull(LB_ROUTER).getSwapOut(PAIR_USDE_USDC, uint128(amountIn), false);
        ( , uint128 expectedSusde, ) = ILBRouterFull(LB_ROUTER).getSwapOut(PAIR_SUSDE_USDE, usdeOut, false);

        uint256 minOut = uint256(expectedSusde) * (10_000 - SLIPPAGE_BPS_CAP) / 10_000;

        // Fund this test with USDC
        deal(USDC, address(this), amountIn);
        IERC20(USDC).approve(address(router), amountIn);

        uint256 susdeBefore = IERC20(SUSDE).balanceOf(address(this));
        uint256 out = router.swapToSusde(amountIn, minOut, block.timestamp + 60, address(this));
        uint256 susdeAfter = IERC20(SUSDE).balanceOf(address(this));

        assertGt(out, 0,                   "swapToSusde returned zero");
        assertEq(susdeAfter - susdeBefore, out, "balance delta != return value");
        assertGe(out, minOut,              "actual < minOut (slippage > 200 bps vs preview)");

        emit log_named_uint("USDC in (1e6)        ", amountIn);
        emit log_named_uint("sUSDe expected (1e18)", expectedSusde);
        emit log_named_uint("sUSDe actual   (1e18)", out);
    }

    // ─── Reverse: sUSDe → USDC ───────────────────────────────────────────────

    function test_SwapFromSusde_LiveMantle() public {
        // First acquire some sUSDe via the forward swap so we have realistic inventory
        uint256 amountInUsdc = 1000e6;
        deal(USDC, address(this), amountInUsdc);
        IERC20(USDC).approve(address(router), amountInUsdc);
        uint256 susdeIn = router.swapToSusde(amountInUsdc, 0, block.timestamp + 60, address(this));
        assertGt(susdeIn, 0, "setup: swapToSusde failed");

        // Expected reverse output: sUSDe→USDe (swapForY=true), then USDe→USDC (swapForY=true)
        ( , uint128 usdeOut, )       = ILBRouterFull(LB_ROUTER).getSwapOut(PAIR_SUSDE_USDE, uint128(susdeIn), true);
        ( , uint128 expectedUsdc, )  = ILBRouterFull(LB_ROUTER).getSwapOut(PAIR_USDE_USDC, usdeOut, true);

        uint256 minOut = uint256(expectedUsdc) * (10_000 - SLIPPAGE_BPS_CAP) / 10_000;

        IERC20(SUSDE).approve(address(router), susdeIn);
        uint256 usdcBefore = IERC20(USDC).balanceOf(address(this));
        uint256 out = router.swapFromSusde(susdeIn, minOut, block.timestamp + 60, address(this));
        uint256 usdcAfter = IERC20(USDC).balanceOf(address(this));

        assertGt(out, 0,                   "swapFromSusde returned zero");
        assertEq(usdcAfter - usdcBefore, out, "balance delta != return value");
        assertGe(out, minOut,              "actual < minOut (slippage > 200 bps vs preview)");

        // Round-trip loss: USDC → sUSDe → USDC should keep most value (~2 LB fees + bin impact)
        // Sanity: out should be > 99% of original amountInUsdc.
        assertGt(out, amountInUsdc * 99 / 100, "round-trip lost > 1%");

        emit log_named_uint("sUSDe in   (1e18)    ", susdeIn);
        emit log_named_uint("USDC expected (1e6)  ", expectedUsdc);
        emit log_named_uint("USDC actual   (1e6)  ", out);
        emit log_named_uint("Round-trip loss bps  ", (amountInUsdc - out) * 10_000 / amountInUsdc);
    }
}
