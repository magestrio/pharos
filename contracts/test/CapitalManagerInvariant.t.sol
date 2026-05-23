// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {CapitalManager} from "../src/CapitalManager.sol";
import {IStrategyAdapter} from "../src/adapters/IStrategyAdapter.sol";
import {CapitalManagerHandler, IMintable} from "./CapitalManagerHandler.sol";

contract InvMockERC20 is ERC20, IMintable {
    constructor() ERC20("Mock USDC", "mUSDC") {}
    function mint(address to, uint256 amount) external override { _mint(to, amount); }
}

/// @notice Honest adapter — `valueInUsdc()` matches held token balance 1:1.
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
    function valueInUsdc() external view override returns (uint256) {
        return underlying.balanceOf(address(this));
    }
}

/// @notice Invariants for the post-vUSDC-pivot CapitalManager. The manager is
/// a raw capital pool — share-token monotonicity invariants live on vUSDC, not
/// here. What we assert at this layer: TA accounting consistency and principal
/// conservation against ghost ledgers.
contract CapitalManagerInvariantTest is Test {
    CapitalManager vault;
    InvMockERC20 token;
    InvHonestAdapter adapterA;
    InvHonestAdapter adapterB;
    CapitalManagerHandler handler;

    address owner = address(0xBEEF);
    address agent = address(0xCAFE);

    function setUp() public {
        token    = new InvMockERC20();
        vault    = new CapitalManager(IERC20(address(token)), owner, address(0));
        adapterA = new InvHonestAdapter(address(token));
        adapterB = new InvHonestAdapter(address(token));

        address[] memory users = new address[](3);
        users[0] = address(0xA11CE);
        users[1] = address(0xB0B);
        users[2] = address(0xCA42);

        handler = new CapitalManagerHandler(vault, token, adapterA, adapterB, owner, agent, users);

        vm.startPrank(owner);
        vault.whitelistStrategy(address(adapterA), true);
        vault.whitelistStrategy(address(adapterB), true);
        vault.setAgent(agent);
        vault.setVusdc(address(handler));
        // Loose slippage caps so fuzz batches aren't dominated by trivial reverts.
        // Honest adapters never produce loss; bound is just defensive.
        vault.setMaxSlippageBps(10000);
        vault.setMaxPerCallLossBps(10000);
        vm.stopPrank();

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

    /// @notice `totalAssetsUsdc()` MUST equal vault free USDC balance plus the
    /// sum of `valueInUsdc()` across every whitelisted adapter.
    function invariant_TotalAssetsConsistency() public view {
        uint256 sum = token.balanceOf(address(vault));
        uint256 n = vault.whitelistedCount();
        for (uint256 i = 0; i < n; ++i) {
            sum += IStrategyAdapter(vault.whitelistedAt(i)).valueInUsdc();
        }
        assertEq(vault.totalAssetsUsdc(), sum, "totalAssetsUsdc != free + sum(value)");
    }

    /// @notice Principal conservation: deposits + yield - withdrawals = AUM.
    /// Honest adapters never destroy value, so equality holds exactly under
    /// this handler's actions.
    function invariant_PrincipalConservation() public view {
        uint256 expected = handler.ghost_totalDeposits()
                         + handler.ghost_totalYield()
                         - handler.ghost_totalWithdrawals();
        assertEq(vault.totalAssetsUsdc(), expected, "principal drift");
    }
}
