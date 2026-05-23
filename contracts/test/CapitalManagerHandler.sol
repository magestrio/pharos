// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {CapitalManager} from "../src/CapitalManager.sol";
import {IStrategyAdapter} from "../src/adapters/IStrategyAdapter.sol";

interface IMintable is IERC20 {
    function mint(address to, uint256 amount) external;
}

/// @notice Bounded fuzz handler for CapitalManager invariant tests under the
/// post-vUSDC pivot: CapitalManager is a raw capital pool, not ERC-4626. The
/// handler itself plays the role of vUSDC for recordDeposit/recordWithdraw.
contract CapitalManagerHandler is Test {
    CapitalManager public immutable vault;
    IMintable public immutable token;
    IStrategyAdapter public immutable adapterA;
    IStrategyAdapter public immutable adapterB;

    address public immutable owner;
    address public immutable agent;
    address[] public users;

    // ─── ghosts ──────────────────────────────────────────────────────────────
    uint256 public ghost_totalDeposits;
    uint256 public ghost_totalWithdrawals;
    uint256 public ghost_totalYield; // tokens minted directly into adapters

    uint256 constant MAX_DEPOSIT = 1_000_000e6;
    uint256 constant MAX_YIELD   = 10_000e6;

    constructor(
        CapitalManager _vault,
        IMintable _token,
        IStrategyAdapter _adapterA,
        IStrategyAdapter _adapterB,
        address _owner,
        address _agent,
        address[] memory _users
    ) {
        vault = _vault;
        token = _token;
        adapterA = _adapterA;
        adapterB = _adapterB;
        owner = _owner;
        agent = _agent;
        users = _users;
    }

    function _pickUser(uint256 seed) internal view returns (address) {
        return users[seed % users.length];
    }

    function _pickAdapter(uint256 seed) internal view returns (IStrategyAdapter) {
        return seed % 2 == 0 ? adapterA : adapterB;
    }

    // ─── actions ─────────────────────────────────────────────────────────────

    /// @notice Simulates a vUSDC mint: this handler IS the vusdc role. Funds
    /// itself and calls recordDeposit against the manager.
    function deposit(uint256 /*seed*/, uint256 amt) external {
        amt = bound(amt, 1, MAX_DEPOSIT);
        token.mint(address(this), amt);
        token.approve(address(vault), amt);
        vault.recordDeposit(amt);
        ghost_totalDeposits += amt;
    }

    /// @notice Simulates a vUSDC redemption: only as much as the manager has
    /// in free cash can be withdrawn. The destination is one of the test users.
    function withdraw(uint256 seed, uint256 amt) external {
        uint256 cash = token.balanceOf(address(vault));
        if (cash == 0) return;
        amt = bound(amt, 1, cash);
        address to = _pickUser(seed);
        vault.recordWithdraw(amt, to);
        ghost_totalWithdrawals += amt;
    }

    function allocate(uint256 seed, uint256 amt) external {
        IStrategyAdapter a = _pickAdapter(seed);
        uint256 free = token.balanceOf(address(vault));
        if (free == 0) return;
        amt = bound(amt, 1, free);

        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = CapitalManager.AllocationCall({
            adapter: address(a),
            kind: CapitalManager.AllocationCallKind.Deposit,
            amount: amt
        });
        vm.prank(agent);
        vault.executeAllocation(bytes32(seed), calls, 0);
    }

    function deallocate(uint256 seed, uint256 amt) external {
        IStrategyAdapter a = _pickAdapter(seed);
        uint256 bal = a.balance();
        if (bal == 0) return;
        amt = bound(amt, 1, bal);

        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = CapitalManager.AllocationCall({
            adapter: address(a),
            kind: CapitalManager.AllocationCallKind.Withdraw,
            amount: amt
        });
        vm.prank(agent);
        vault.executeAllocation(bytes32(seed), calls, 0);
    }

    /// @notice Simulates external yield accruing in an adapter (e.g. Aave
    /// interest). Tokens are minted directly to the adapter, raising its
    /// `valueInUsdc()` without touching vault free balance.
    function accrueYield(uint256 seed, uint256 amt) external {
        IStrategyAdapter a = _pickAdapter(seed);
        amt = bound(amt, 1, MAX_YIELD);
        token.mint(address(a), amt);
        ghost_totalYield += amt;
    }

    // ─── view helpers ────────────────────────────────────────────────────────

    function usersLength() external view returns (uint256) {
        return users.length;
    }
}
