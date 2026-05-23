// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {CapitalManager} from "../src/CapitalManager.sol";
import {IStrategyAdapter} from "../src/adapters/IStrategyAdapter.sol";
import {CapitalManagerHandler, IMintable} from "./CapitalManagerHandler.sol";

contract InvMockERC20 is ERC20, IMintable {
    constructor() ERC20("Mock", "MOCK") {}
    function mint(address to, uint256 amount) external override { _mint(to, amount); }
}

/// @notice Honest adapter — `valueInBaseAsset()` matches held token balance 1:1.
/// Used so share-price-monotonic invariant has a chance to hold (no oracle
/// loss). Lossy / bad-oracle adapters are covered in unit/slippage suites.
contract InvHonestAdapter is IStrategyAdapter {
    IERC20 public immutable underlying;
    constructor(address _asset) { underlying = IERC20(_asset); }
    function deposit(uint256 amount) external override {
        underlying.transferFrom(msg.sender, address(this), amount);
    }
    function withdraw(uint256 amount) external override returns (uint256) {
        underlying.transfer(msg.sender, amount);
        return amount;
    }
    function balance() external view override returns (uint256) {
        return underlying.balanceOf(address(this));
    }
    function asset() external view override returns (address) { return address(underlying); }
    function valueInBaseAsset() external view override returns (uint256) {
        return underlying.balanceOf(address(this));
    }
}

contract CapitalManagerInvariantTest is Test {
    CapitalManager vault;
    InvMockERC20 token;
    InvHonestAdapter adapterA;
    InvHonestAdapter adapterB;
    CapitalManagerHandler handler;

    address owner = address(0xBEEF);
    address agent = address(0xCAFE);

    function setUp() public {
        token   = new InvMockERC20();
        vault   = new CapitalManager(IERC20(address(token)), owner, "V", "v", address(0));
        adapterA = new InvHonestAdapter(address(token));
        adapterB = new InvHonestAdapter(address(token));

        vm.startPrank(owner);
        vault.whitelistStrategy(address(adapterA), true);
        vault.whitelistStrategy(address(adapterB), true);
        vault.setAgent(agent);
        // Loose slippage caps so fuzz batches aren't dominated by trivial reverts.
        // Honest adapters never produce loss; bound is just defensive.
        vault.setMaxSlippageBps(10000);
        vault.setMaxPerCallLossBps(10000);
        vm.stopPrank();

        address[] memory users = new address[](3);
        users[0] = address(0xA11CE);
        users[1] = address(0xB0B);
        users[2] = address(0xCA42);

        handler = new CapitalManagerHandler(vault, token, adapterA, adapterB, owner, agent, users);

        targetContract(address(handler));

        bytes4[] memory selectors = new bytes4[](5);
        selectors[0] = CapitalManagerHandler.deposit.selector;
        selectors[1] = CapitalManagerHandler.withdraw.selector;
        selectors[2] = CapitalManagerHandler.allocate.selector;
        selectors[3] = CapitalManagerHandler.deallocate.selector;
        selectors[4] = CapitalManagerHandler.accrueYield.selector;
        targetSelector(FuzzSelector({addr: address(handler), selectors: selectors}));
    }

    // ─── invariants ──────────────────────────────────────────────────────────

    /// @notice `totalAssets()` MUST equal vault free balance plus the sum of
    /// `valueInBaseAsset()` across every whitelisted adapter — the very
    /// definition `totalAssets()` is implemented to honour. Regression guard.
    function invariant_TotalAssetsConsistency() public view {
        uint256 sum = token.balanceOf(address(vault));
        uint256 n = vault.whitelistedCount();
        for (uint256 i = 0; i < n; ++i) {
            sum += IStrategyAdapter(vault.whitelistedAt(i)).valueInBaseAsset();
        }
        assertEq(vault.totalAssets(), sum, "totalAssets != free + sum(value)");
    }

    /// @notice Under honest adapters with strictly-non-negative yield, the
    /// ERC-4626 share price (`convertToAssets(1e18)`) must never decrease
    /// between observations. The handler updates `ghost_sharePriceDecreased`
    /// the moment a decrement is seen.
    function invariant_SharePriceMonotonic() public view {
        assertFalse(
            handler.ghost_sharePriceDecreased(),
            "share price decreased between handler calls"
        );
    }

    /// @notice Share price never falls below the anchor (price observed
    /// immediately after the first deposit). With honest adapters + yield
    /// only, the anchor is a non-decreasing lower bound.
    function invariant_SharePriceAboveAnchor() public view {
        if (!handler.ghost_anchorSet() || vault.totalSupply() == 0) return;
        uint256 cur = vault.convertToAssets(1 ether);
        assertGe(cur, handler.ghost_anchorSharePrice(), "below anchor");
    }

    /// @notice `totalAssets()` is conserved: deposited capital plus accrued
    /// yield minus user withdrawals is the upper bound on AUM. Honest
    /// adapters don't destroy value, so equality holds up to ERC-4626
    /// integer-division rounding (≤ users.length wei tolerance).
    function invariant_PrincipalConservation() public view {
        uint256 expected = handler.ghost_totalDeposits() + handler.ghost_totalYield()
                         - handler.ghost_totalWithdrawals();
        // Tolerance: one wei of rounding error per active user per withdrawal cycle.
        // Each `withdraw` may round shares down by 1 wei against the user; this
        // accumulates as a residual in the vault. Bound generously.
        assertApproxEqAbs(vault.totalAssets(), expected, handler.usersLength() + 1, "principal drift");
    }

    /// @notice Whenever there are outstanding shares, the vault must hold
    /// some assets backing them. The reverse direction (supply == 0 implies
    /// assets == 0) does NOT hold for ERC-4626 in general: when the last
    /// depositor exits, accrued yield they didn't proportionally earn can
    /// strand as dust — fixable in production by pre-minting dead shares,
    /// out of scope for this invariant.
    function invariant_SharesBackedByAssets() public view {
        if (vault.totalSupply() > 0) {
            assertGt(vault.totalAssets(), 0, "supply >0 but assets 0");
        }
    }
}
