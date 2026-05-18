// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {AaveV3WethAdapter} from "../src/adapters/AaveV3WethAdapter.sol";

contract MockERC20 is ERC20 {
    constructor(string memory name, string memory symbol) ERC20(name, symbol) {}
    function mint(address to, uint256 amount) external { _mint(to, amount); }
}

contract MockAavePoolV2 {
    // underlying => aToken mapping
    mapping(address => address) public aTokenFor;

    function setAToken(address underlying, address aToken) external {
        aTokenFor[underlying] = aToken;
    }

    function supply(address asset, uint256 amount, address onBehalfOf, uint16) external {
        // pull underlying from caller (adapter approved this pool)
        IERC20(asset).transferFrom(msg.sender, address(this), amount);
        // mint aToken 1:1
        MockERC20(aTokenFor[asset]).mint(onBehalfOf, amount);
    }

    function withdraw(address asset, uint256 amount, address to) external returns (uint256) {
        // mint underlying back (simulating Aave releasing funds)
        MockERC20(asset).mint(to, amount);
        return amount;
    }
}

contract AaveV3WethAdapterTest is Test {
    MockERC20        weth;
    MockERC20        aWeth;
    MockAavePoolV2   pool;
    AaveV3WethAdapter adapter;

    address vault = address(this); // test contract acts as vault
    address owner = address(0xBEEF);

    function setUp() public {
        weth  = new MockERC20("Wrapped Ether", "WETH");
        aWeth = new MockERC20("Aave WETH", "aWETH");
        pool  = new MockAavePoolV2();
        pool.setAToken(address(weth), address(aWeth));

        adapter = new AaveV3WethAdapter(
            address(pool),
            address(weth),
            address(aWeth),
            vault,
            owner
        );
    }

    function test_Deploy() public view {
        assertEq(address(adapter.aavePool()), address(pool));
        assertEq(address(adapter.weth()), address(weth));
        assertEq(address(adapter.aWeth()), address(aWeth));
        assertEq(adapter.vault(), vault);
        assertEq(adapter.owner(), owner);
        assertEq(adapter.asset(), address(weth));
    }

    function test_OnlyVault_Deposit() public {
        vm.prank(address(0xDEAD));
        vm.expectRevert("not vault");
        adapter.deposit(1e18);
    }

    function test_OnlyVault_Withdraw() public {
        vm.prank(address(0xDEAD));
        vm.expectRevert("not vault");
        adapter.withdraw(1e18);
    }

    function test_Deposit_SuppliesToAave() public {
        uint256 amount = 1e18;
        weth.mint(vault, amount);
        weth.approve(address(adapter), amount);

        adapter.deposit(amount);

        assertEq(weth.balanceOf(vault), 0);
        assertEq(weth.balanceOf(address(pool)), amount);
        assertEq(aWeth.balanceOf(address(adapter)), amount);
        assertEq(adapter.balance(), amount);
    }

    function test_Withdraw_ReturnsToVault() public {
        uint256 amount = 1e18;
        // set up: deposit first
        weth.mint(vault, amount);
        weth.approve(address(adapter), amount);
        adapter.deposit(amount);

        adapter.withdraw(amount);

        assertEq(weth.balanceOf(vault), amount);
    }
}
