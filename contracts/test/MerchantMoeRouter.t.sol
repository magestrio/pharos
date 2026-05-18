// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {MerchantMoeRouter} from "../src/adapters/MerchantMoeRouter.sol";
import {ILBRouter} from "../src/adapters/interfaces/ILBRouter.sol";

contract MockERC20 is ERC20 {
    constructor(string memory name, string memory symbol) ERC20(name, symbol) {}
    function mint(address to, uint256 amount) external { _mint(to, amount); }
}

// Fixed rate: 1 WETH (1e18) = 2000 USDC (2000e6)
contract MockLBRouter {
    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256, /* amountOutMin */
        ILBRouter.Path memory path,
        address to,
        uint256 /* deadline */
    ) external returns (uint256 amountOut) {
        IERC20 tokenIn  = path.tokenPath[0];
        IERC20 tokenOut = path.tokenPath[path.tokenPath.length - 1];

        tokenIn.transferFrom(msg.sender, address(this), amountIn);

        // WETH (18 dec) → USDC (6 dec): amountOut = amountIn * 2000e6 / 1e18
        // USDC (6 dec)  → WETH (18 dec): amountOut = amountIn * 1e18 / 2000e6
        bool wethToUsdc = ERC20(address(tokenOut)).decimals() == 6;
        amountOut = wethToUsdc
            ? amountIn * 2000e6 / 1e18
            : amountIn * 1e18 / 2000e6;

        MockERC20Decimals(address(tokenOut)).mint(to, amountOut);
    }
}

contract MerchantMoeRouterTest is Test {
    MockERC20         weth;
    MockERC20Decimals usdc;
    MockLBRouter      lbRouter;
    MerchantMoeRouter router;

    address owner = address(0xBEEF);
    address user  = address(0xCAFE);

    function setUp() public {
        weth     = new MockERC20("Wrapped Ether", "WETH");
        usdc     = new MockERC20Decimals("USD Coin", "USDC", 6);
        lbRouter = new MockLBRouter();
        router   = new MerchantMoeRouter(address(lbRouter), address(weth), address(usdc), owner);
    }

    function test_Deploy() public view {
        assertEq(address(router.lbRouter()), address(lbRouter));
        assertEq(address(router.weth()), address(weth));
        assertEq(address(router.usdc()), address(usdc));
        assertEq(router.owner(), owner);
        assertEq(router.maxSlippageBps(), 200);
    }

    function test_SwapWethToUsdc() public {
        uint256 amountIn = 1e18; // 1 WETH
        weth.mint(user, amountIn);

        vm.startPrank(user);
        weth.approve(address(router), amountIn);
        uint256 out = router.swapWethToUsdc(amountIn, 0, block.timestamp + 60);
        vm.stopPrank();

        assertEq(out, 2000e6); // 1 WETH = 2000 USDC
        assertEq(usdc.balanceOf(user), 2000e6);
        assertEq(weth.balanceOf(user), 0);
    }

    function test_SwapUsdcToWeth() public {
        uint256 amountIn = 2000e6; // 2000 USDC
        usdc.mint(user, amountIn);

        vm.startPrank(user);
        usdc.approve(address(router), amountIn);
        uint256 out = router.swapUsdcToWeth(amountIn, 0, block.timestamp + 60);
        vm.stopPrank();

        assertEq(out, 1e18); // 2000 USDC = 1 WETH
        assertEq(weth.balanceOf(user), 1e18);
        assertEq(usdc.balanceOf(user), 0);
    }

    function test_SetMaxSlippageBps_OnlyOwner() public {
        vm.prank(address(0xDEAD));
        vm.expectRevert();
        router.setMaxSlippageBps(500);

        vm.prank(owner);
        router.setMaxSlippageBps(500);
        assertEq(router.maxSlippageBps(), 500);
    }

    function test_SetMaxSlippageBps_MaxCap() public {
        vm.prank(owner);
        vm.expectRevert("max 10%");
        router.setMaxSlippageBps(1001);
    }
}

// Helper to create ERC20 with custom decimals
contract MockERC20Decimals is ERC20 {
    uint8 private _dec;
    constructor(string memory name, string memory symbol, uint8 dec) ERC20(name, symbol) { _dec = dec; }
    function decimals() public view override returns (uint8) { return _dec; }
    function mint(address to, uint256 amount) external { _mint(to, amount); }
}
