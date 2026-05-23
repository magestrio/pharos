// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test, console} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {CapitalManager} from "../src/CapitalManager.sol";
import {IStrategyAdapter} from "../src/adapters/IStrategyAdapter.sol";

contract MockERC20 is ERC20 {
    constructor() ERC20("Mock USDC", "mUSDC") {}
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
    // Test contract plays the role of vUSDC for recordDeposit/recordWithdraw.
    address vusdcRole;
    address user  = address(0xBEEF);
    address other = address(0xDEAD);

    function setUp() public {
        vusdcRole = address(this);

        token    = new MockERC20();
        vault    = new CapitalManager(IERC20(address(token)), owner, address(0));
        honestA  = new HonestAdapter(address(token));
        honestB  = new HonestAdapter(address(token));
        reverter = new RevertingAdapter(address(token), "reverter: nope");

        vault.whitelistStrategy(address(honestA),  true);
        vault.whitelistStrategy(address(honestB),  true);
        vault.whitelistStrategy(address(reverter), true);
        vault.setAgent(agent);
        vault.setVusdc(vusdcRole);

        // Raise slippage caps so they don't fire in path-coverage tests.
        // Slippage logic is exercised separately in CapitalManagerSlippage.t.sol.
        vault.setMaxSlippageBps(10000);
        vault.setMaxPerCallLossBps(10000);
    }

    /// @notice Helper: simulate a user mint of vUSDC by funding the vusdc role
    /// with `amt` tokens, approving the vault, and calling recordDeposit.
    function _recordDeposit(uint256 amt) internal {
        token.mint(vusdcRole, amt);
        token.approve(address(vault), amt);
        vault.recordDeposit(amt);
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
        assertEq(address(vault.usdc()), address(token));
        assertEq(vault.owner(), owner);
        assertEq(vault.agent(), agent);
        assertEq(vault.vusdc(), vusdcRole);
    }

    function test_Constructor_RevertsOnZeroUsdc() public {
        vm.expectRevert("zero usdc");
        new CapitalManager(IERC20(address(0)), owner, address(0));
    }

    function test_Constructor_StoresSequencerFeed() public {
        CapitalManager v = new CapitalManager(IERC20(address(token)), owner, address(0xFEED));
        assertEq(v.sequencerUptimeFeed(), address(0xFEED));
    }

    function test_Constructor_DefaultsBps() public {
        CapitalManager v = new CapitalManager(IERC20(address(token)), owner, address(0));
        assertEq(v.maxSlippageBps(),    100);
        assertEq(v.maxPerCallLossBps(), 100);
    }

    // ═══ setVusdc ═════════════════════════════════════════════════════════════

    function test_SetVusdc_OnlyOwner() public {
        CapitalManager v = new CapitalManager(IERC20(address(token)), owner, address(0));
        vm.prank(user);
        vm.expectRevert();
        v.setVusdc(address(0xABCD));
    }

    function test_SetVusdc_RevertsOnZero() public {
        CapitalManager v = new CapitalManager(IERC20(address(token)), owner, address(0));
        vm.expectRevert("zero vusdc");
        v.setVusdc(address(0));
    }

    function test_SetVusdc_OneShot() public {
        // setUp() already called setVusdc — a second call must revert.
        vm.expectRevert("vusdc set");
        vault.setVusdc(address(0xABCD));
    }

    function test_SetVusdc_EmitsEvent() public {
        CapitalManager v = new CapitalManager(IERC20(address(token)), owner, address(0));
        vm.expectEmit(true, false, false, true);
        emit CapitalManager.VusdcSet(address(0xABCD));
        v.setVusdc(address(0xABCD));
        assertEq(v.vusdc(), address(0xABCD));
    }

    // ═══ recordDeposit ════════════════════════════════════════════════════════

    function test_RecordDeposit_OnlyVusdc() public {
        token.mint(other, 1e6);
        vm.startPrank(other);
        token.approve(address(vault), 1e6);
        vm.expectRevert("not vusdc");
        vault.recordDeposit(1e6);
        vm.stopPrank();
    }

    function test_RecordDeposit_RevertsOnZero() public {
        vm.expectRevert("zero amount");
        vault.recordDeposit(0);
    }

    function test_RecordDeposit_PullsUsdc() public {
        _recordDeposit(1000e6);
        assertEq(token.balanceOf(address(vault)), 1000e6);
        assertEq(token.balanceOf(vusdcRole), 0);
        assertEq(vault.totalAssetsUsdc(), 1000e6);
    }

    function test_RecordDeposit_EmitsEvent() public {
        token.mint(vusdcRole, 500e6);
        token.approve(address(vault), 500e6);
        vm.expectEmit(false, false, false, true);
        emit CapitalManager.DepositRecorded(500e6);
        vault.recordDeposit(500e6);
    }

    function test_RecordDeposit_Paused() public {
        vault.pause();
        token.mint(vusdcRole, 1e6);
        token.approve(address(vault), 1e6);
        vm.expectRevert();
        vault.recordDeposit(1e6);
    }

    // ═══ recordWithdraw ═══════════════════════════════════════════════════════

    function test_RecordWithdraw_OnlyVusdc() public {
        _recordDeposit(1000e6);
        vm.prank(other);
        vm.expectRevert("not vusdc");
        vault.recordWithdraw(100e6, user);
    }

    function test_RecordWithdraw_RevertsOnZeroAmount() public {
        _recordDeposit(1000e6);
        vm.expectRevert("zero amount");
        vault.recordWithdraw(0, user);
    }

    function test_RecordWithdraw_RevertsOnZeroTo() public {
        _recordDeposit(1000e6);
        vm.expectRevert("zero to");
        vault.recordWithdraw(100e6, address(0));
    }

    function test_RecordWithdraw_TransfersUsdc() public {
        _recordDeposit(1000e6);
        vault.recordWithdraw(400e6, user);
        assertEq(token.balanceOf(user), 400e6);
        assertEq(token.balanceOf(address(vault)), 600e6);
        assertEq(vault.totalAssetsUsdc(), 600e6);
    }

    function test_RecordWithdraw_EmitsEvent() public {
        _recordDeposit(1000e6);
        vm.expectEmit(false, true, false, true);
        emit CapitalManager.WithdrawRecorded(250e6, user);
        vault.recordWithdraw(250e6, user);
    }

    function test_RecordWithdraw_Paused() public {
        _recordDeposit(1000e6);
        vault.pause();
        vm.expectRevert();
        vault.recordWithdraw(100e6, user);
    }

    function test_RecordWithdraw_InsufficientCashReverts() public {
        // Less in cash than withdraw amount → SafeERC20 revert.
        _recordDeposit(100e6);
        vm.expectRevert();
        vault.recordWithdraw(200e6, user);
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
        _recordDeposit(1000e6);
        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = _depositCall(address(honestA), 600e6);

        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(1)), calls, 0);

        assertEq(token.balanceOf(address(vault)),    400e6);
        assertEq(token.balanceOf(address(honestA)),  600e6);
        assertEq(vault.totalAssetsUsdc(),            1000e6);
    }

    function test_ExecuteAllocation_SingleWithdraw() public {
        _recordDeposit(1000e6);
        CapitalManager.AllocationCall[] memory deposits = new CapitalManager.AllocationCall[](1);
        deposits[0] = _depositCall(address(honestA), 700e6);
        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(1)), deposits, 0);

        CapitalManager.AllocationCall[] memory withdraws = new CapitalManager.AllocationCall[](1);
        withdraws[0] = _withdrawCall(address(honestA), 200e6);
        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(2)), withdraws, 0);

        assertEq(token.balanceOf(address(vault)),    500e6);
        assertEq(token.balanceOf(address(honestA)),  500e6);
    }

    function test_ExecuteAllocation_MultiCallMix_DepositThenWithdraw() public {
        _recordDeposit(1000e6);

        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](2);
        calls[0] = _depositCall(address(honestA), 600e6);
        calls[1] = _withdrawCall(address(honestA), 100e6);

        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(1)), calls, 0);

        assertEq(token.balanceOf(address(vault)),   500e6);
        assertEq(token.balanceOf(address(honestA)), 500e6);
    }

    function test_ExecuteAllocation_MultiAdapter() public {
        _recordDeposit(1000e6);

        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](2);
        calls[0] = _depositCall(address(honestA), 300e6);
        calls[1] = _depositCall(address(honestB), 200e6);

        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(7)), calls, 0);

        assertEq(token.balanceOf(address(honestA)), 300e6);
        assertEq(token.balanceOf(address(honestB)), 200e6);
        assertEq(token.balanceOf(address(vault)),   500e6);
    }

    function test_ExecuteAllocation_EmitsCallExecutedAndAllocationExecuted() public {
        _recordDeposit(1000e6);
        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = _depositCall(address(honestA), 500e6);

        vm.expectEmit(true, true, true, true);
        emit CapitalManager.CallExecuted(
            bytes32(uint256(99)),
            0,
            address(honestA),
            CapitalManager.AllocationCallKind.Deposit,
            500e6,
            500e6
        );
        vm.expectEmit(true, false, false, true);
        emit CapitalManager.AllocationExecuted(bytes32(uint256(99)), 1000e6);

        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(99)), calls, 0);
    }

    // ═══ executeAllocation — rejection paths ══════════════════════════════════

    function test_ExecuteAllocation_OnlyAgent() public {
        _recordDeposit(1000e6);
        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = _depositCall(address(honestA), 100e6);

        vm.expectRevert("not agent");
        vm.prank(user);
        vault.executeAllocation(bytes32(0), calls, 0);
    }

    function test_ExecuteAllocation_Paused() public {
        _recordDeposit(1000e6);
        vault.pause();
        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = _depositCall(address(honestA), 100e6);

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
        _recordDeposit(1000e6);
        HonestAdapter rogue = new HonestAdapter(address(token));

        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = _depositCall(address(rogue), 100e6);

        vm.expectRevert("not whitelisted");
        vm.prank(agent);
        vault.executeAllocation(bytes32(0), calls, 0);
    }

    /// @notice 2-call batch where the second call reverts. The whole tx must
    /// roll back — the vault and the first adapter must look untouched.
    function test_ExecuteAllocation_PartialRevert_RollsBackPreviousCalls() public {
        _recordDeposit(1000e6);

        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](2);
        calls[0] = _depositCall(address(honestA),  500e6);
        calls[1] = _depositCall(address(reverter), 100e6);

        vm.expectRevert("reverter: nope");
        vm.prank(agent);
        vault.executeAllocation(bytes32(0), calls, 0);

        // call[0] must NOT have persisted.
        assertEq(token.balanceOf(address(vault)),    1000e6);
        assertEq(token.balanceOf(address(honestA)),  0);
        assertEq(token.balanceOf(address(reverter)), 0);
    }

    function test_ExecuteAllocation_MinTotalAssetsAfter_Reverts() public {
        _recordDeposit(1000e6);
        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = _depositCall(address(honestA), 100e6);

        // 1000 TA after the call, agent demands 2000 → revert.
        vm.expectRevert("slippage");
        vm.prank(agent);
        vault.executeAllocation(bytes32(0), calls, 2000e6);
    }

    // ═══ emergencyWithdraw ════════════════════════════════════════════════════

    function test_EmergencyWithdraw_OnlyOwner() public {
        vm.expectRevert();
        vm.prank(user);
        vault.emergencyWithdraw(address(honestA));
    }

    function test_EmergencyWithdraw_PullsFullBalance() public {
        _recordDeposit(1000e6);
        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = _depositCall(address(honestA), 400e6);
        vm.prank(agent);
        vault.executeAllocation(bytes32(0), calls, 0);

        assertEq(token.balanceOf(address(honestA)), 400e6);
        vault.emergencyWithdraw(address(honestA));
        assertEq(token.balanceOf(address(honestA)), 0);
        assertEq(token.balanceOf(address(vault)),   1000e6);
    }

    function test_EmergencyWithdraw_ZeroBalance_NoOp() public {
        // Adapter has no balance — no-op, no revert.
        vault.emergencyWithdraw(address(honestA));
        assertEq(token.balanceOf(address(vault)), 0);
    }

    function test_EmergencyWithdraw_EmitsEvent() public {
        _recordDeposit(100e6);
        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = _depositCall(address(honestA), 60e6);
        vm.prank(agent);
        vault.executeAllocation(bytes32(0), calls, 0);

        vm.expectEmit(true, false, false, true);
        emit CapitalManager.EmergencyWithdrawn(address(honestA), 60e6);
        vault.emergencyWithdraw(address(honestA));
    }

    function test_EmergencyWithdraw_BypassesPause() public {
        _recordDeposit(100e6);
        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = _depositCall(address(honestA), 60e6);
        vm.prank(agent);
        vault.executeAllocation(bytes32(0), calls, 0);

        vault.pause();
        // Should still succeed when paused — emergency lever.
        vault.emergencyWithdraw(address(honestA));
        assertEq(token.balanceOf(address(vault)), 100e6);
    }

    // ═══ Sequencer uptime check ═══════════════════════════════════════════════

    function test_Sequencer_NoFeed_NoOp() public view {
        // setUp() built vault with address(0) feed.
        // totalAssetsUsdc() must succeed without invoking sequencer logic.
        vault.totalAssetsUsdc();
    }

    function test_Sequencer_FeedUp_TotalAssetsOk() public {
        // Move ahead so block.timestamp - startedAt > 1h grace.
        vm.warp(10_000);
        MockSequencerFeed feed = new MockSequencerFeed(0, 1); // up, very old start
        CapitalManager v = new CapitalManager(IERC20(address(token)), owner, address(feed));
        v.totalAssetsUsdc(); // should not revert
    }

    function test_Sequencer_Down_TotalAssetsReverts() public {
        MockSequencerFeed feed = new MockSequencerFeed(1, 1); // DOWN
        CapitalManager v = new CapitalManager(IERC20(address(token)), owner, address(feed));
        vm.expectRevert("sequencer down");
        v.totalAssetsUsdc();
    }

    function test_Sequencer_RecentlyRestored_TotalAssetsReverts() public {
        // Sequencer up, but restarted seconds ago — still within grace window.
        vm.warp(10_000);
        MockSequencerFeed feed = new MockSequencerFeed(0, 9_999);
        CapitalManager v = new CapitalManager(IERC20(address(token)), owner, address(feed));
        vm.expectRevert("sequencer grace");
        v.totalAssetsUsdc();
    }

    function test_Sequencer_GraceElapsed_TotalAssetsOk() public {
        // Sequencer up for longer than the 1h grace.
        vm.warp(10_000);
        MockSequencerFeed feed = new MockSequencerFeed(0, 1);
        CapitalManager v = new CapitalManager(IERC20(address(token)), owner, address(feed));
        v.totalAssetsUsdc(); // 9999s > 3600s grace → ok
    }
}
