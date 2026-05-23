// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test, console} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {CapitalManager} from "../src/CapitalManager.sol";
import {IStrategyAdapter} from "../src/adapters/IStrategyAdapter.sol";

contract MockERC20 is ERC20 {
    constructor() ERC20("Mock", "MOCK") {}
    function mint(address to, uint256 amount) external { _mint(to, amount); }
}

/// @notice Honest adapter — 1:1 between balance and valueInUsdc.
contract HonestAdapter is IStrategyAdapter {
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
    function asset() external view override returns (address) {
        return address(underlying);
    }
    function valueInUsdc() external view override returns (uint256) {
        return underlying.balanceOf(address(this));
    }
}

/// @notice Adapter that reverts on deposit — for partial-revert atomicity tests.
contract RevertingAdapter is IStrategyAdapter {
    IERC20 public immutable underlying;
    string  public revertReason;

    constructor(address _asset, string memory _reason) {
        underlying = IERC20(_asset);
        revertReason = _reason;
    }

    function deposit(uint256) external view override {
        require(false, revertReason);
    }
    function withdraw(uint256) external view override returns (uint256) {
        require(false, revertReason);
    }
    function balance() external pure override returns (uint256) { return 0; }
    function asset() external view override returns (address) { return address(underlying); }
    function valueInUsdc() external pure override returns (uint256) { return 0; }
}

/// @notice Controllable Chainlink-style sequencer-uptime feed mock.
contract MockSequencerFeed {
    int256 public answer;       // 0 = up, 1 = down
    uint256 public startedAt;   // last status change timestamp

    constructor(int256 _answer, uint256 _startedAt) {
        answer = _answer;
        startedAt = _startedAt;
    }

    function setAnswer(int256 _a) external { answer = _a; }
    function setStartedAt(uint256 _s) external { startedAt = _s; }

    function latestRoundData() external view returns (uint80, int256, uint256, uint256, uint80) {
        return (0, answer, startedAt, 0, 0);
    }
}

contract CapitalManagerTest is Test {
    CapitalManager vault;
    MockERC20 token;
    HonestAdapter honestA;
    HonestAdapter honestB;
    RevertingAdapter reverter;

    address owner = address(this);
    address agent = address(0xCAFE);
    address user  = address(0xBEEF);
    address other = address(0xDEAD);

    function setUp() public {
        token    = new MockERC20();
        vault    = new CapitalManager(IERC20(address(token)), owner, "Vault", "vM", address(0));
        honestA  = new HonestAdapter(address(token));
        honestB  = new HonestAdapter(address(token));
        reverter = new RevertingAdapter(address(token), "reverter: nope");

        vault.whitelistStrategy(address(honestA),  true);
        vault.whitelistStrategy(address(honestB),  true);
        vault.whitelistStrategy(address(reverter), true);
        vault.setAgent(agent);

        // Raise slippage caps so they don't fire in path-coverage tests.
        // Slippage logic is exercised separately in CapitalManagerSlippage.t.sol.
        vault.setMaxSlippageBps(10000);
        vault.setMaxPerCallLossBps(10000);
    }

    function _deposit(address who, uint256 amt) internal {
        token.mint(who, amt);
        vm.startPrank(who);
        token.approve(address(vault), amt);
        vault.deposit(amt, who);
        vm.stopPrank();
    }

    function _depositCall(address adapter_, uint256 amt) internal pure returns (CapitalManager.AllocationCall memory) {
        return CapitalManager.AllocationCall({
            adapter: adapter_,
            kind: CapitalManager.AllocationCallKind.Deposit,
            amount: amt
        });
    }

    function _withdrawCall(address adapter_, uint256 amt) internal pure returns (CapitalManager.AllocationCall memory) {
        return CapitalManager.AllocationCall({
            adapter: adapter_,
            kind: CapitalManager.AllocationCallKind.Withdraw,
            amount: amt
        });
    }

    // ═══ Deployment / defaults ════════════════════════════════════════════════

    function test_Deploy_Basics() public view {
        assertEq(vault.asset(),  address(token));
        assertEq(vault.name(),   "Vault");
        assertEq(vault.symbol(), "vM");
        assertEq(vault.owner(),  owner);
        assertEq(vault.agent(),  agent);
    }

    function test_Constructor_StoresSequencerFeed() public {
        CapitalManager v = new CapitalManager(
            IERC20(address(token)), owner, "V", "v", address(0xFEED)
        );
        assertEq(v.sequencerUptimeFeed(), address(0xFEED));
    }

    function test_Constructor_DefaultsBps() public {
        CapitalManager v = new CapitalManager(
            IERC20(address(token)), owner, "V", "v", address(0)
        );
        assertEq(v.maxSlippageBps(),    100);
        assertEq(v.maxPerCallLossBps(), 100);
    }

    // ═══ ERC-4626 entry points ════════════════════════════════════════════════

    function test_Deposit_MintsSharesOneToOne() public {
        _deposit(user, 1000 ether);
        assertEq(vault.balanceOf(user), 1000 ether);
        assertEq(token.balanceOf(address(vault)), 1000 ether);
        assertEq(vault.totalAssets(), 1000 ether);
    }

    function test_Withdraw_BurnsShares() public {
        _deposit(user, 1000 ether);
        vm.prank(user);
        vault.withdraw(500 ether, user, user);
        assertEq(vault.balanceOf(user), 500 ether);
        assertEq(token.balanceOf(user), 500 ether);
    }

    function test_Mint_Works() public {
        token.mint(user, 1000 ether);
        vm.startPrank(user);
        token.approve(address(vault), 1000 ether);
        vault.mint(1000 ether, user);
        vm.stopPrank();
        assertEq(vault.balanceOf(user), 1000 ether);
    }

    function test_Redeem_Works() public {
        _deposit(user, 1000 ether);
        vm.prank(user);
        vault.redeem(400 ether, user, user);
        assertEq(vault.balanceOf(user), 600 ether);
        assertEq(token.balanceOf(user), 400 ether);
    }

    function test_Paused_BlocksDeposit() public {
        vault.pause();
        token.mint(user, 1 ether);
        vm.startPrank(user);
        token.approve(address(vault), 1 ether);
        vm.expectRevert();
        vault.deposit(1 ether, user);
        vm.stopPrank();
    }

    function test_Paused_BlocksWithdraw() public {
        _deposit(user, 1 ether);
        vault.pause();
        vm.prank(user);
        vm.expectRevert();
        vault.withdraw(1 ether, user, user);
    }

    // ═══ Owner functions ══════════════════════════════════════════════════════

    function test_SetAgent_OnlyOwner() public {
        vm.expectRevert();
        vm.prank(user);
        vault.setAgent(other);
    }

    function test_SetAgent_RevertsOnZero() public {
        vm.expectRevert("zero agent");
        vault.setAgent(address(0));
    }

    function test_WhitelistStrategy_OnlyOwner() public {
        vm.expectRevert();
        vm.prank(user);
        vault.whitelistStrategy(address(honestA), false);
    }

    function test_WhitelistStrategy_RevertsOnZero() public {
        vm.expectRevert("zero strategy");
        vault.whitelistStrategy(address(0), true);
    }

    function test_WhitelistStrategy_AddRemove() public {
        HonestAdapter h = new HonestAdapter(address(token));
        assertFalse(vault.isWhitelisted(address(h)));

        vault.whitelistStrategy(address(h), true);
        assertTrue(vault.isWhitelisted(address(h)));

        vault.whitelistStrategy(address(h), false);
        assertFalse(vault.isWhitelisted(address(h)));
    }

    function test_Pause_OnlyOwner() public {
        vm.expectRevert();
        vm.prank(user);
        vault.pause();
    }

    function test_Unpause_OnlyOwner() public {
        vault.pause();
        vm.expectRevert();
        vm.prank(user);
        vault.unpause();
    }

    // ═══ Whitelist views ══════════════════════════════════════════════════════

    function test_WhitelistedCount_TracksAdds() public {
        // setUp() already whitelisted 3.
        assertEq(vault.whitelistedCount(), 3);
        HonestAdapter h = new HonestAdapter(address(token));
        vault.whitelistStrategy(address(h), true);
        assertEq(vault.whitelistedCount(), 4);
        vault.whitelistStrategy(address(h), false);
        assertEq(vault.whitelistedCount(), 3);
    }

    function test_WhitelistedAt_Iterates() public view {
        address[] memory seen = new address[](3);
        for (uint256 i = 0; i < 3; ++i) seen[i] = vault.whitelistedAt(i);
        // Order is insertion order under EnumerableSet on add-only setup.
        assertEq(seen[0], address(honestA));
        assertEq(seen[1], address(honestB));
        assertEq(seen[2], address(reverter));
    }

    // ═══ executeAllocation — happy paths ══════════════════════════════════════

    function test_ExecuteAllocation_SingleDeposit() public {
        _deposit(user, 1000 ether);
        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = _depositCall(address(honestA), 600 ether);

        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(1)), calls, 0);

        assertEq(token.balanceOf(address(vault)),    400 ether);
        assertEq(token.balanceOf(address(honestA)),  600 ether);
        assertEq(vault.totalAssets(),                1000 ether);
    }

    function test_ExecuteAllocation_SingleWithdraw() public {
        _deposit(user, 1000 ether);
        CapitalManager.AllocationCall[] memory deposits = new CapitalManager.AllocationCall[](1);
        deposits[0] = _depositCall(address(honestA), 700 ether);
        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(1)), deposits, 0);

        CapitalManager.AllocationCall[] memory withdraws = new CapitalManager.AllocationCall[](1);
        withdraws[0] = _withdrawCall(address(honestA), 200 ether);
        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(2)), withdraws, 0);

        assertEq(token.balanceOf(address(vault)),    500 ether);
        assertEq(token.balanceOf(address(honestA)),  500 ether);
    }

    function test_ExecuteAllocation_MultiCallMix_DepositThenWithdraw() public {
        _deposit(user, 1000 ether);

        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](2);
        calls[0] = _depositCall(address(honestA), 600 ether);
        calls[1] = _withdrawCall(address(honestA), 100 ether);

        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(1)), calls, 0);

        assertEq(token.balanceOf(address(vault)),   500 ether);
        assertEq(token.balanceOf(address(honestA)), 500 ether);
    }

    function test_ExecuteAllocation_MultiAdapter() public {
        _deposit(user, 1000 ether);

        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](2);
        calls[0] = _depositCall(address(honestA), 300 ether);
        calls[1] = _depositCall(address(honestB), 200 ether);

        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(7)), calls, 0);

        assertEq(token.balanceOf(address(honestA)), 300 ether);
        assertEq(token.balanceOf(address(honestB)), 200 ether);
        assertEq(token.balanceOf(address(vault)),   500 ether);
    }

    function test_ExecuteAllocation_EmitsCallExecutedAndAllocationExecuted() public {
        _deposit(user, 1000 ether);
        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = _depositCall(address(honestA), 500 ether);

        vm.expectEmit(true, true, true, true);
        emit CapitalManager.CallExecuted(
            bytes32(uint256(99)),
            0,
            address(honestA),
            CapitalManager.AllocationCallKind.Deposit,
            500 ether,
            500 ether
        );
        vm.expectEmit(true, false, false, true);
        emit CapitalManager.AllocationExecuted(bytes32(uint256(99)), 1000 ether);

        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(99)), calls, 0);
    }

    // ═══ executeAllocation — rejection paths ══════════════════════════════════

    function test_ExecuteAllocation_OnlyAgent() public {
        _deposit(user, 1000 ether);
        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = _depositCall(address(honestA), 100 ether);

        vm.expectRevert("not agent");
        vm.prank(user);
        vault.executeAllocation(bytes32(0), calls, 0);
    }

    function test_ExecuteAllocation_Paused() public {
        _deposit(user, 1000 ether);
        vault.pause();
        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = _depositCall(address(honestA), 100 ether);

        vm.expectRevert();
        vm.prank(agent);
        vault.executeAllocation(bytes32(0), calls, 0);
    }

    function test_ExecuteAllocation_EmptyCalls_Reverts() public {
        CapitalManager.AllocationCall[] memory empty = new CapitalManager.AllocationCall[](0);
        vm.expectRevert("empty calls");
        vm.prank(agent);
        vault.executeAllocation(bytes32(0), empty, 0);
    }

    function test_ExecuteAllocation_NonWhitelistedAdapter_Reverts() public {
        _deposit(user, 1000 ether);
        HonestAdapter rogue = new HonestAdapter(address(token));

        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = _depositCall(address(rogue), 100 ether);

        vm.expectRevert("not whitelisted");
        vm.prank(agent);
        vault.executeAllocation(bytes32(0), calls, 0);
    }

    /// @notice 2-call batch where the second call reverts. The whole tx must
    /// roll back — the vault and the first adapter must look untouched.
    function test_ExecuteAllocation_PartialRevert_RollsBackPreviousCalls() public {
        _deposit(user, 1000 ether);

        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](2);
        calls[0] = _depositCall(address(honestA),  500 ether);
        calls[1] = _depositCall(address(reverter), 100 ether);

        vm.expectRevert("reverter: nope");
        vm.prank(agent);
        vault.executeAllocation(bytes32(0), calls, 0);

        // call[0] must NOT have persisted.
        assertEq(token.balanceOf(address(vault)),    1000 ether);
        assertEq(token.balanceOf(address(honestA)),  0);
        assertEq(token.balanceOf(address(reverter)), 0);
    }

    function test_ExecuteAllocation_MinTotalAssetsAfter_Reverts() public {
        _deposit(user, 1000 ether);
        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = _depositCall(address(honestA), 100 ether);

        // 1000 TA after the call, agent demands 2000 → revert.
        vm.expectRevert("slippage");
        vm.prank(agent);
        vault.executeAllocation(bytes32(0), calls, 2000 ether);
    }

    // ═══ emergencyWithdraw ════════════════════════════════════════════════════

    function test_EmergencyWithdraw_OnlyOwner() public {
        vm.expectRevert();
        vm.prank(user);
        vault.emergencyWithdraw(address(honestA));
    }

    function test_EmergencyWithdraw_PullsFullBalance() public {
        _deposit(user, 1000 ether);
        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = _depositCall(address(honestA), 400 ether);
        vm.prank(agent);
        vault.executeAllocation(bytes32(0), calls, 0);

        assertEq(token.balanceOf(address(honestA)), 400 ether);
        vault.emergencyWithdraw(address(honestA));
        assertEq(token.balanceOf(address(honestA)), 0);
        assertEq(token.balanceOf(address(vault)),   1000 ether);
    }

    function test_EmergencyWithdraw_ZeroBalance_NoOp() public {
        // Adapter has no balance — no-op, no revert.
        vault.emergencyWithdraw(address(honestA));
        assertEq(token.balanceOf(address(vault)), 0);
    }

    function test_EmergencyWithdraw_EmitsEvent() public {
        _deposit(user, 100 ether);
        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = _depositCall(address(honestA), 60 ether);
        vm.prank(agent);
        vault.executeAllocation(bytes32(0), calls, 0);

        vm.expectEmit(true, false, false, true);
        emit CapitalManager.EmergencyWithdrawn(address(honestA), 60 ether);
        vault.emergencyWithdraw(address(honestA));
    }

    function test_EmergencyWithdraw_BypassesPause() public {
        _deposit(user, 100 ether);
        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = _depositCall(address(honestA), 60 ether);
        vm.prank(agent);
        vault.executeAllocation(bytes32(0), calls, 0);

        vault.pause();
        // Should still succeed when paused — emergency lever.
        vault.emergencyWithdraw(address(honestA));
        assertEq(token.balanceOf(address(vault)), 100 ether);
    }

    // ═══ Sequencer uptime check ═══════════════════════════════════════════════

    function test_Sequencer_NoFeed_NoOp() public view {
        // setUp() built vault with address(0) feed.
        // totalAssets() must succeed without invoking sequencer logic.
        vault.totalAssets();
    }

    function test_Sequencer_FeedUp_TotalAssetsOk() public {
        // Move ahead so block.timestamp - startedAt > 1h grace.
        vm.warp(10_000);
        MockSequencerFeed feed = new MockSequencerFeed(0, 1); // up, very old start
        CapitalManager v = new CapitalManager(
            IERC20(address(token)), owner, "V", "v", address(feed)
        );
        v.totalAssets(); // should not revert
    }

    function test_Sequencer_Down_TotalAssetsReverts() public {
        MockSequencerFeed feed = new MockSequencerFeed(1, 1); // DOWN
        CapitalManager v = new CapitalManager(
            IERC20(address(token)), owner, "V", "v", address(feed)
        );
        vm.expectRevert("sequencer down");
        v.totalAssets();
    }

    function test_Sequencer_RecentlyRestored_TotalAssetsReverts() public {
        // Sequencer up, but restarted seconds ago — still within grace window.
        vm.warp(10_000);
        MockSequencerFeed feed = new MockSequencerFeed(0, 9_999);
        CapitalManager v = new CapitalManager(
            IERC20(address(token)), owner, "V", "v", address(feed)
        );
        vm.expectRevert("sequencer grace");
        v.totalAssets();
    }

    function test_Sequencer_GraceElapsed_TotalAssetsOk() public {
        // Sequencer up for longer than the 1h grace.
        vm.warp(10_000);
        MockSequencerFeed feed = new MockSequencerFeed(0, 1);
        CapitalManager v = new CapitalManager(
            IERC20(address(token)), owner, "V", "v", address(feed)
        );
        v.totalAssets(); // 9999s > 3600s grace → ok
    }
}
