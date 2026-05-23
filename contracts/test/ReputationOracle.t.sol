// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {ReputationOracle} from "../src/ReputationOracle.sol";

// ─── Mocks ───────────────────────────────────────────────────────────────────

contract MockVault {
    uint256 private _assets;
    function setAssets(uint256 a) external { _assets = a; }
    function totalAssetsUsdc() external view returns (uint256) { return _assets; }
}

contract MockRegistry {
    struct Feedback {
        uint256 agentId;
        int128  value;
        uint8   decimals;
        string  tag1;
        string  tag2;
    }

    Feedback[] private _feedbacks;

    function giveFeedback(
        uint256 agentId_,
        int128 value,
        uint8 dec,
        string calldata t1,
        string calldata t2,
        string calldata,
        string calldata,
        bytes32
    ) external {
        _feedbacks.push(Feedback(agentId_, value, dec, t1, t2));
    }

    function count() external view returns (uint256) { return _feedbacks.length; }

    function get(uint256 i) external view returns (Feedback memory) { return _feedbacks[i]; }
}

// ─── Tests ───────────────────────────────────────────────────────────────────

contract ReputationOracleTest is Test {
    MockVault    vault;
    MockRegistry reg;
    ReputationOracle oracle;

    uint256 constant AGENT_ID        = 42;
    uint256 constant MIN_INTERVAL    = 1 hours;
    uint256 constant SECONDS_PER_YEAR = 365 days;

    address constant ALICE = address(0xA11CE);
    address constant BOB   = address(0xB0B);

    function setUp() public {
        vault  = new MockVault();
        reg    = new MockRegistry();
        oracle = new ReputationOracle(address(vault), address(reg), AGENT_ID);
    }

    // ── Constructor ──────────────────────────────────────────────────────────

    function test_Constructor_RevertZeroVault() public {
        vm.expectRevert(ReputationOracle.ZeroAddress.selector);
        new ReputationOracle(address(0), address(reg), AGENT_ID);
    }

    function test_Constructor_RevertZeroRegistry() public {
        vm.expectRevert(ReputationOracle.ZeroAddress.selector);
        new ReputationOracle(address(vault), address(0), AGENT_ID);
    }

    function test_Constructor_ImmutablesCorrect() public view {
        assertEq(address(oracle.vault()),    address(vault));
        assertEq(address(oracle.registry()), address(reg));
        assertEq(oracle.agentId(),           AGENT_ID);
        assertEq(oracle.baselineAssets(),    0);
        assertEq(oracle.updateCount(),       0);
    }

    // ── Empty vault guards ───────────────────────────────────────────────────

    function test_UpdateReputation_RevertVaultEmpty() public {
        vault.setAssets(0);
        vm.expectRevert(ReputationOracle.VaultEmpty.selector);
        oracle.updateReputation();
    }

    function test_CanUpdate_FalseWhenEmpty() public view {
        assertFalse(oracle.canUpdate());
    }

    // ── First call / baseline ────────────────────────────────────────────────

    function test_FirstCall_SetsBaselineScoreZero() public {
        vault.setAssets(100e18);

        vm.expectEmit(false, false, false, true);
        emit ReputationOracle.BaselineSet(100e18, block.timestamp);

        int128 score = oracle.updateReputation();

        assertEq(score, 0);
        assertEq(oracle.baselineAssets(), 100e18);
        assertEq(oracle.updateCount(), 1);
        assertEq(reg.count(), 1);

        MockRegistry.Feedback memory fb = reg.get(0);
        assertEq(fb.agentId,  AGENT_ID);
        assertEq(fb.value,    0);
        assertEq(fb.decimals, 2);
        assertEq(fb.tag1,     "apr");
        assertEq(fb.tag2,     "cumulative");
    }

    function test_BaselineImmutableAfterFirstCall() public {
        vault.setAssets(100e18);
        oracle.updateReputation();

        uint256 snapshotBaseline = oracle.baselineAssets();

        vault.setAssets(200e18); // assets changed
        // baseline must NOT change
        assertEq(oracle.baselineAssets(), snapshotBaseline);
    }

    // ── Anti-spam ────────────────────────────────────────────────────────────

    function test_AntiSpam_RevertTooSoon() public {
        vault.setAssets(100e18);
        oracle.updateReputation(); // sets baseline

        uint256 expectedNext = block.timestamp + MIN_INTERVAL;
        vm.expectRevert(abi.encodeWithSelector(ReputationOracle.TooSoon.selector, expectedNext));
        oracle.updateReputation();
    }

    function test_UpdateAfterInterval_Succeeds() public {
        vault.setAssets(100e18);
        oracle.updateReputation();

        vm.warp(block.timestamp + MIN_INTERVAL);
        vault.setAssets(105e18);

        int128 score = oracle.updateReputation();
        assertGt(score, 0);
        assertEq(oracle.updateCount(), 2);
    }

    // ── APR math ─────────────────────────────────────────────────────────────

    function test_GainCase_1Year_10pct() public {
        vault.setAssets(100e18);
        oracle.updateReputation(); // baseline = 100e18

        vm.warp(block.timestamp + SECONDS_PER_YEAR);
        vault.setAssets(110e18);

        int128 score = oracle.updateReputation();
        // diff=10e18, bps = 10e18 * 10_000 * 365d / (100e18 * 365d) = 1000
        assertEq(score, 1000);
    }

    function test_LossCase_1Year_10pct() public {
        vault.setAssets(100e18);
        oracle.updateReputation();

        vm.warp(block.timestamp + SECONDS_PER_YEAR);
        vault.setAssets(90e18);

        int128 score = oracle.updateReputation();
        assertEq(score, -1000);
    }

    function test_ShortTimeframe_1Day_1pct_Annualized() public {
        vault.setAssets(100e18);
        oracle.updateReputation();

        vm.warp(block.timestamp + 1 days);
        vault.setAssets(101e18);

        int128 score = oracle.updateReputation();
        // diff=1e18, bps = 1e18 * 10_000 * 365d / (100e18 * 1d) = 36500
        assertEq(score, 36500);
    }

    // ── Preview / canUpdate ──────────────────────────────────────────────────

    function test_PreviewScore_NoStorageWrite() public {
        vault.setAssets(100e18);
        oracle.updateReputation();

        vm.warp(block.timestamp + SECONDS_PER_YEAR);
        vault.setAssets(110e18);

        uint256 countBefore    = reg.count();
        uint256 updateCountBefore = oracle.updateCount();

        (int128 preview,,) = oracle.previewScore();
        assertEq(preview, 1000);
        assertEq(reg.count(),         countBefore);       // no registry push
        assertEq(oracle.updateCount(), updateCountBefore); // no state write
    }

    function test_CanUpdate_TrueAfterInterval() public {
        vault.setAssets(100e18);
        oracle.updateReputation(); // baseline

        assertFalse(oracle.canUpdate()); // inside cooldown

        vm.warp(block.timestamp + MIN_INTERVAL);
        assertTrue(oracle.canUpdate());
    }

    // ── Overflow guard ───────────────────────────────────────────────────────

    function test_ScoreOverflow_ExtremeValues() public {
        vault.setAssets(1e18);
        oracle.updateReputation(); // baseline = 1e18

        // Must warp past MIN_INTERVAL or the call reverts with TooSoon instead.
        // elapsed = 3600s; diff = 1e50 → bps ~ 8.75e39 > int128.max (~1.7e38)
        vm.warp(block.timestamp + MIN_INTERVAL);
        vault.setAssets(1e18 + 1e50);

        vm.expectRevert(ReputationOracle.ScoreOverflow.selector);
        oracle.updateReputation();
    }

    // ── Permissionless ───────────────────────────────────────────────────────

    function test_AnyCallerCanUpdate() public {
        vault.setAssets(100e18);

        vm.prank(ALICE);
        oracle.updateReputation(); // ALICE sets baseline

        vm.warp(block.timestamp + SECONDS_PER_YEAR);
        vault.setAssets(110e18);

        // BOB's call should succeed and be recorded as caller in the event
        vm.expectEmit(true, true, false, false);
        emit ReputationOracle.ReputationUpdated(2, BOB, 0, 0, 0);

        vm.prank(BOB);
        int128 score = oracle.updateReputation();

        assertEq(score, 1000);
        assertEq(oracle.updateCount(), 2);
    }
}
