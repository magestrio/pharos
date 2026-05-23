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

/// @notice Bounded fuzz handler for Vault8004 invariant tests under the
/// multi-call execution architecture. Wraps every public mutating action in
/// size + actor bounds so the fuzzer explores meaningful state instead of
/// drowning in trivial reverts. Maintains ghost variables that downstream
/// invariants assert against.
contract Vault8004Handler is Test {
    Vault8004 public immutable vault;
    IMintable public immutable token;
    IStrategyAdapter public immutable adapterA;
    IStrategyAdapter public immutable adapterB;

    address public immutable owner;
    address public immutable agent;
    address[] public users;

    // ─── ghosts ──────────────────────────────────────────────────────────────
    uint256 public ghost_totalDeposits;
    uint256 public ghost_totalWithdrawals;
    uint256 public ghost_totalYield;       // tokens minted directly into adapters
    uint256 public ghost_anchorSharePrice; // share price right after first deposit
    bool    public ghost_anchorSet;
    bool    public ghost_sharePriceDecreased; // sticky: ever decreased between observations
    uint256 public ghost_lastSharePrice;

    uint256 constant MAX_DEPOSIT = 1_000_000 ether;
    uint256 constant MAX_YIELD   = 10_000 ether;

    constructor(
        Vault8004 _vault,
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

    /// @dev Observe share price and update ghost trackers. Called after every
    /// state-mutating action so monotonicity violations are caught immediately.
    function _observeSharePrice() internal {
        if (vault.totalSupply() == 0) return;
        uint256 cur = vault.convertToAssets(1 ether);

        if (!ghost_anchorSet) {
            ghost_anchorSharePrice = cur;
            ghost_lastSharePrice = cur;
            ghost_anchorSet = true;
            return;
        }
        if (cur < ghost_lastSharePrice) {
            ghost_sharePriceDecreased = true;
        }
        ghost_lastSharePrice = cur;
    }

    // ─── actions ─────────────────────────────────────────────────────────────

    function deposit(uint256 seed, uint256 amt) external {
        amt = bound(amt, 1, MAX_DEPOSIT);
        address u = _pickUser(seed);
        token.mint(u, amt);
        vm.startPrank(u);
        token.approve(address(vault), amt);
        vault.deposit(amt, u);
        vm.stopPrank();
        ghost_totalDeposits += amt;
        _observeSharePrice();
    }

    function withdraw(uint256 seed, uint256 amt) external {
        address u = _pickUser(seed);
        uint256 maxW = vault.maxWithdraw(u);
        if (maxW == 0) return;
        amt = bound(amt, 1, maxW);
        vm.prank(u);
        vault.withdraw(amt, u, u);
        ghost_totalWithdrawals += amt;
        _observeSharePrice();
    }

    function allocate(uint256 seed, uint256 amt) external {
        // Single-Deposit executeAllocation against one of the two adapters.
        IStrategyAdapter a = _pickAdapter(seed);
        uint256 free = token.balanceOf(address(vault));
        if (free == 0) return;
        amt = bound(amt, 1, free);

        Vault8004.AllocationCall[] memory calls = new Vault8004.AllocationCall[](1);
        calls[0] = Vault8004.AllocationCall({
            adapter: address(a),
            kind: Vault8004.AllocationCallKind.Deposit,
            amount: amt
        });
        vm.prank(agent);
        vault.executeAllocation(bytes32(seed), calls, 0);
        _observeSharePrice();
    }

    function deallocate(uint256 seed, uint256 amt) external {
        IStrategyAdapter a = _pickAdapter(seed);
        uint256 bal = a.balance();
        if (bal == 0) return;
        amt = bound(amt, 1, bal);

        Vault8004.AllocationCall[] memory calls = new Vault8004.AllocationCall[](1);
        calls[0] = Vault8004.AllocationCall({
            adapter: address(a),
            kind: Vault8004.AllocationCallKind.Withdraw,
            amount: amt
        });
        vm.prank(agent);
        vault.executeAllocation(bytes32(seed), calls, 0);
        _observeSharePrice();
    }

    /// @notice Simulates external yield accruing in an adapter (e.g. Aave
    /// interest). Tokens are minted directly to the adapter, raising its
    /// `valueInBaseAsset()` without touching vault free balance.
    function accrueYield(uint256 seed, uint256 amt) external {
        // Yield only makes sense once a depositor exists — otherwise we'd create
        // assets without backing shares and break ERC-4626 sanity.
        if (vault.totalSupply() == 0) return;
        IStrategyAdapter a = _pickAdapter(seed);
        amt = bound(amt, 1, MAX_YIELD);
        token.mint(address(a), amt);
        ghost_totalYield += amt;
        _observeSharePrice();
    }

    // ─── view helpers ────────────────────────────────────────────────────────

    function usersLength() external view returns (uint256) {
        return users.length;
    }
}
