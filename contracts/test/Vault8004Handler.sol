// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {Vault8004} from "../src/Vault8004.sol";
import {IStrategyAdapter} from "../src/adapters/IStrategyAdapter.sol";

interface IMintable is IERC20 {
    function mint(address to, uint256 amount) external;
}

/// @notice Bounded fuzz handler for Vault8004 invariant tests.
/// Wraps every public mutating action in size + actor bounds so the fuzzer
/// explores a meaningful state space without trivial reverts dominating runs.
contract Vault8004Handler is Test {
    Vault8004 public immutable vault;
    IMintable public immutable token;
    IStrategyAdapter public immutable strategy;

    address public immutable owner;
    address public immutable agent;

    address[] public users;
    mapping(address => uint256) public userDeposited;

    // ghost
    uint256 public ghost_totalDeposits;
    uint256 public ghost_totalWithdrawals;

    uint256 constant MAX_DEPOSIT = 1_000_000 ether;
    uint256 constant MAX_YIELD   = 10_000 ether;

    constructor(
        Vault8004 _vault,
        IMintable _token,
        IStrategyAdapter _strategy,
        address _owner,
        address _agent,
        address[] memory _users
    ) {
        vault = _vault;
        token = _token;
        strategy = _strategy;
        owner = _owner;
        agent = _agent;
        users = _users;
    }

    function _pickUser(uint256 seed) internal view returns (address) {
        return users[seed % users.length];
    }

    // ── actions ──────────────────────────────────────────────────────────────

    function deposit(uint256 seed, uint256 amt) external {
        amt = bound(amt, 1, MAX_DEPOSIT);
        address u = _pickUser(seed);
        token.mint(u, amt);
        vm.startPrank(u);
        token.approve(address(vault), amt);
        vault.deposit(amt, u);
        vm.stopPrank();
        userDeposited[u] += amt;
        ghost_totalDeposits += amt;
    }

    function withdraw(uint256 seed, uint256 amt) external {
        address u = _pickUser(seed);
        uint256 maxWithdraw = vault.maxWithdraw(u);
        if (maxWithdraw == 0) return;
        amt = bound(amt, 1, maxWithdraw);
        vm.prank(u);
        vault.withdraw(amt, u, u);
        ghost_totalWithdrawals += amt;
    }

    function allocate(uint256 amt) external {
        uint256 free = token.balanceOf(address(vault)) - vault.totalAllocated();
        if (free == 0) return;
        amt = bound(amt, 1, free);
        vm.prank(agent);
        vault.allocate(bytes32(uint256(amt)), amt);
    }

    function deallocate(uint256 amt) external {
        uint256 bal = strategy.balance();
        if (bal == 0) return;
        // Allow over-request to exercise the clamp path.
        amt = bound(amt, 1, bal * 2);
        vm.prank(agent);
        vault.deallocate(bytes32(uint256(amt)), amt);
    }

    function accrueYield(uint256 amt) external {
        amt = bound(amt, 1, MAX_YIELD);
        token.mint(address(strategy), amt);
    }

    // ── invariant accounting helpers ─────────────────────────────────────────

    function usersLength() external view returns (uint256) {
        return users.length;
    }
}
