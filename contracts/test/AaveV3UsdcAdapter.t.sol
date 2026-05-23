// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {AaveV3UsdcAdapter} from "../src/adapters/AaveV3UsdcAdapter.sol";

contract MockERC20 is ERC20 {
    constructor(string memory name, string memory symbol) ERC20(name, symbol) {}
    function mint(address to, uint256 amount) external { _mint(to, amount); }
}

contract MockAavePool {
    mapping(address => address) public aTokenFor;

    function setAToken(address underlying, address aToken) external {
        aTokenFor[underlying] = aToken;
    }

    function supply(address asset, uint256 amount, address onBehalfOf, uint16) external {
        IERC20(asset).transferFrom(msg.sender, address(this), amount);
        MockERC20(aTokenFor[asset]).mint(onBehalfOf, amount);
    }

    function withdraw(address asset, uint256 amount, address to) external returns (uint256) {
        MockERC20(asset).mint(to, amount);
        return amount;
    }
}

contract AaveV3UsdcAdapterTest is Test {
    MockERC20        usdc;
    MockERC20        aUsdc;
    MockAavePool     pool;
    AaveV3UsdcAdapter adapter;

    address vault = address(this);
    address owner = address(0xBEEF);

    function setUp() public {
        usdc  = new MockERC20("USD Coin", "USDC");
        aUsdc = new MockERC20("Aave USDC", "aUSDC");
        pool  = new MockAavePool();
        pool.setAToken(address(usdc), address(aUsdc));

        adapter = new AaveV3UsdcAdapter(
            address(pool),
            address(usdc),
            address(aUsdc),
            vault,
            owner
        );
    }

    function test_Deploy() public view {
        assertEq(address(adapter.aavePool()), address(pool));
        assertEq(address(adapter.usdc()), address(usdc));
        assertEq(address(adapter.aUsdc()), address(aUsdc));
        assertEq(adapter.vault(), vault);
        assertEq(adapter.owner(), owner);
        assertEq(adapter.asset(), address(usdc));
    }

    function test_OnlyVault_Deposit() public {
        vm.prank(address(0xDEAD));
        vm.expectRevert("not vault");
        adapter.deposit(1e6);
    }

    function test_OnlyVault_Withdraw() public {
        vm.prank(address(0xDEAD));
        vm.expectRevert("not vault");
        adapter.withdraw(1e6);
    }

    function test_Deposit_SuppliesToAave() public {
        uint256 amount = 1000e6; // 1000 USDC
        usdc.mint(vault, amount);
        usdc.approve(address(adapter), amount);

        adapter.deposit(amount);

        assertEq(usdc.balanceOf(vault), 0);
        assertEq(usdc.balanceOf(address(pool)), amount);
        assertEq(aUsdc.balanceOf(address(adapter)), amount);
        assertEq(adapter.balance(), amount);
        assertEq(adapter.valueInUsdc(), amount, "valueInUsdc tracks aUSDC balance 1:1");
    }

    function test_Withdraw_ReturnsToVault() public {
        uint256 amount = 1000e6;
        usdc.mint(vault, amount);
        usdc.approve(address(adapter), amount);
        adapter.deposit(amount);

        adapter.withdraw(amount);

        assertEq(usdc.balanceOf(vault), amount);
    }

    function test_ValueInUsdc_ZeroOnEmpty() public view {
        assertEq(adapter.valueInUsdc(), 0);
    }
}
