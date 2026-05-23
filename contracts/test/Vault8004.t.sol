// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {Vault8004} from "../src/Vault8004.sol";
import {IStrategyAdapter} from "../src/adapters/IStrategyAdapter.sol";

contract MockERC20 is ERC20 {
    constructor() ERC20("Mock mETH", "mETH") {}
    function mint(address to, uint256 amount) external { _mint(to, amount); }
}

contract MockStrategy is IStrategyAdapter {
    IERC20 private immutable _asset;

    constructor(address asset_) {
        _asset = IERC20(asset_);
    }

    function deposit(uint256 amount) external override {
        _asset.transferFrom(msg.sender, address(this), amount);
    }

    function withdraw(uint256 amount) external override returns (uint256) {
        _asset.transfer(msg.sender, amount);
        return amount;
    }

    function balance() external view override returns (uint256) {
        return _asset.balanceOf(address(this));
    }

    function asset() external view override returns (address) {
        return address(_asset);
    }
}

/// @notice Simulates Aave-style 1 wei round-down on deposit. balance() always
/// reports `depositAmount - 1`. Used to verify Vault8004.deallocate clamps the
/// requested amount to adapter.balance() instead of reverting.
contract RoundingDownStrategy is IStrategyAdapter {
    IERC20 private immutable _asset;
    address constant BURN = address(0xdEaD);

    constructor(address asset_) {
        _asset = IERC20(asset_);
    }

    function deposit(uint256 amount) external override {
        _asset.transferFrom(msg.sender, address(this), amount);
        _asset.transfer(BURN, 1);
    }

    function withdraw(uint256 amount) external override returns (uint256) {
        _asset.transfer(msg.sender, amount);
        return amount;
    }

    function balance() external view override returns (uint256) {
        return _asset.balanceOf(address(this));
    }

    function asset() external view override returns (address) {
        return address(_asset);
    }
}

contract Vault8004Test is Test {
    Vault8004 vault;
    MockERC20 meth;
    MockStrategy strategy;

    address owner = address(0xBEEF);
    address agent = address(0xCAFE);
    address user  = address(0xDEAD);

    function setUp() public {
        meth     = new MockERC20();
        vault    = new Vault8004(IERC20(address(meth)), owner, "Vault mETH", "vmETH");
        strategy = new MockStrategy(address(meth));
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    function _setupStrategy() internal {
        vm.startPrank(owner);
        vault.whitelistStrategy(address(strategy), true);
        vault.setCurrentStrategy(address(strategy));
        vault.setAgent(agent);
        vm.stopPrank();
    }

    function _depositAs(address who, uint256 amount) internal {
        meth.mint(who, amount);
        vm.startPrank(who);
        meth.approve(address(vault), amount);
        vault.deposit(amount, who);
        vm.stopPrank();
    }

    // ── tests ─────────────────────────────────────────────────────────────────

    function test_Deploy() public view {
        assertEq(vault.asset(), address(meth));
        assertEq(vault.name(), "Vault mETH");
        assertEq(vault.symbol(), "vmETH");
    }

    function test_DepositMintsShares() public {
        _depositAs(user, 1000 ether);
        // first deposit → 1:1 share ratio
        assertEq(vault.balanceOf(user), 1000 ether);
        assertEq(meth.balanceOf(address(vault)), 1000 ether);
    }

    function test_WithdrawBurnsShares() public {
        _depositAs(user, 1000 ether);

        vm.prank(user);
        vault.withdraw(500 ether, user, user);

        assertEq(vault.balanceOf(user), 500 ether);
        assertEq(meth.balanceOf(user), 500 ether);
    }

    function test_SetAgent_OnlyOwner() public {
        vm.expectRevert();
        vm.prank(user);
        vault.setAgent(agent);

        vm.prank(owner);
        vault.setAgent(agent);
        assertEq(vault.agent(), agent);
    }

    function test_WhitelistStrategy_OnlyOwner() public {
        vm.expectRevert();
        vm.prank(user);
        vault.whitelistStrategy(address(strategy), true);

        vm.prank(owner);
        vault.whitelistStrategy(address(strategy), true);
        assertTrue(vault.whitelistedStrategies(address(strategy)));
    }

    function test_Allocate_OnlyAgent() public {
        _setupStrategy();
        _depositAs(user, 1000 ether);

        vm.expectRevert("not agent");
        vm.prank(user);
        vault.allocate(bytes32(0), 500 ether);
    }

    function test_Allocate_RequiresWhitelist() public {
        // agent set but strategy NOT whitelisted or set
        vm.prank(owner);
        vault.setAgent(agent);

        _depositAs(user, 1000 ether);

        vm.expectRevert("no strategy");
        vm.prank(agent);
        vault.allocate(bytes32(0), 500 ether);
    }

    function test_Allocate_TransfersToStrategy() public {
        _setupStrategy();
        _depositAs(user, 1000 ether);

        vm.prank(agent);
        vault.allocate(bytes32(uint256(1)), 600 ether);

        assertEq(strategy.balance(), 600 ether);
        assertEq(vault.totalAllocated(), 600 ether);
        assertEq(meth.balanceOf(address(vault)), 400 ether);
    }

    function test_Deallocate_ReturnsToVault() public {
        _setupStrategy();
        _depositAs(user, 1000 ether);

        vm.prank(agent);
        vault.allocate(bytes32(uint256(1)), 600 ether);

        vm.prank(agent);
        vault.deallocate(bytes32(uint256(2)), 600 ether);

        assertEq(vault.totalAllocated(), 0);
        assertEq(meth.balanceOf(address(vault)), 1000 ether);
        assertEq(strategy.balance(), 0);
    }

    function test_TotalAssets_IncludesStrategy() public {
        _setupStrategy();
        _depositAs(user, 1000 ether);

        vm.prank(agent);
        vault.allocate(bytes32(uint256(1)), 400 ether);

        // vault cash: 600, strategy: 400
        assertEq(vault.totalAssets(), 1000 ether);
    }

    function test_Paused_BlocksDeposit() public {
        vm.prank(owner);
        vault.pause();

        meth.mint(user, 100 ether);
        vm.startPrank(user);
        meth.approve(address(vault), 100 ether);
        vm.expectRevert();
        vault.deposit(100 ether, user);
        vm.stopPrank();
    }

    function test_Paused_BlocksAllocate() public {
        _setupStrategy();
        _depositAs(user, 1000 ether);

        vm.prank(owner);
        vault.pause();

        vm.expectRevert();
        vm.prank(agent);
        vault.allocate(bytes32(0), 500 ether);
    }

    function test_Deallocate_ClampsToAdapterBalance() public {
        // Strategy loses 1 wei to rounding on deposit (Aave-style aToken minting).
        RoundingDownStrategy lossy = new RoundingDownStrategy(address(meth));
        vm.startPrank(owner);
        vault.whitelistStrategy(address(lossy), true);
        vault.setCurrentStrategy(address(lossy));
        vault.setAgent(agent);
        vm.stopPrank();

        _depositAs(user, 1000 ether);

        vm.prank(agent);
        vault.allocate(bytes32(uint256(1)), 600 ether);

        // balance() now reports 600 ether - 1 due to rounding burn.
        assertEq(lossy.balance(), 600 ether - 1);
        assertEq(vault.totalAllocated(), 600 ether);

        // Agent tries to pull the full original deposit amount — without clamping
        // this would revert. With clamping, the vault pulls what the adapter holds.
        vm.prank(agent);
        vault.deallocate(bytes32(uint256(2)), 600 ether);

        // Full exit: adapter empty → totalAllocated zeroed (no phantom debt).
        assertEq(lossy.balance(), 0);
        assertEq(vault.totalAllocated(), 0);
        assertEq(meth.balanceOf(address(vault)), 1000 ether - 1);
    }

    function test_Deallocate_HandlesAccruedInterest() public {
        _setupStrategy();
        _depositAs(user, 1000 ether);

        vm.prank(agent);
        vault.allocate(bytes32(uint256(1)), 600 ether);

        // Simulate yield: extra 50 mETH appears in strategy (Aave interest).
        meth.mint(address(strategy), 50 ether);
        assertEq(strategy.balance(), 650 ether);
        assertEq(vault.totalAllocated(), 600 ether);

        // Agent pulls the full balance (more than totalAllocated).
        vm.prank(agent);
        vault.deallocate(bytes32(uint256(2)), 650 ether);

        // Vault receives the yield; bookkeeping zeroed correctly (no underflow).
        assertEq(strategy.balance(), 0);
        assertEq(vault.totalAllocated(), 0);
        assertEq(meth.balanceOf(address(vault)), 1050 ether);
    }

    function test_Deallocate_OverRequestClampsWithoutRevert() public {
        // Agent requests more than the strategy holds at all — clamp should pull
        // whatever's available rather than reverting.
        _setupStrategy();
        _depositAs(user, 1000 ether);

        vm.prank(agent);
        vault.allocate(bytes32(uint256(1)), 400 ether);

        vm.prank(agent);
        vault.deallocate(bytes32(uint256(2)), 999 ether);

        assertEq(strategy.balance(), 0);
        assertEq(vault.totalAllocated(), 0);
        assertEq(meth.balanceOf(address(vault)), 1000 ether);
    }

    function test_EmergencyWithdraw_PullsFromStrategy() public {
        _setupStrategy();
        _depositAs(user, 1000 ether);

        vm.prank(agent);
        vault.allocate(bytes32(uint256(1)), 500 ether);

        assertEq(vault.totalAllocated(), 500 ether);
        assertEq(strategy.balance(), 500 ether);

        vm.prank(owner);
        vault.emergencyWithdraw(address(strategy));

        assertEq(vault.totalAllocated(), 0);
        assertEq(strategy.balance(), 0);
        assertEq(meth.balanceOf(address(vault)), 1000 ether);
    }
}
