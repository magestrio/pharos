// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {CapitalManager} from "../src/CapitalManager.sol";
import {BybitAttestor} from "../src/adapters/BybitAttestor.sol";
import {IStrategyAdapter} from "../src/adapters/IStrategyAdapter.sol";

/// @notice Verifies BybitAttestor plugs into CapitalManager as a normal
///         IStrategyAdapter. Covers the wiring `.17` calls for:
///           - whitelistStrategy succeeds
///           - totalAssetsUsdc aggregates BybitAttestor.valueInUsdc
///           - executeAllocation Deposit routes USDC to attestor escrow
///           - executeAllocation Withdraw is async (returns 0, no USDC moves)
///           - fail-loud staleness propagates from attestor → vault view
contract MockERC20 is ERC20 {
    constructor() ERC20("Mock USDC", "mUSDC") {}
    function mint(address to, uint256 amount) external { _mint(to, amount); }
}

contract BybitAttestorIntegrationTest is Test {
    MockERC20      usdc;
    CapitalManager vault;
    BybitAttestor  attestorAdapter;

    address owner    = address(this);
    address agent    = address(0xCAFE);
    address attestor = address(0xA77E);
    address vusdcRole;

    bytes32 constant DECISION_ID = bytes32(uint256(0xDEC));
    uint256 constant SEED = 1_000 * 1e6; // 1000 USDC seeded into vault

    // Mirror events for vm.expectEmit.
    event DepositRequested(uint256 indexed txId, uint256 amount);
    event WithdrawRequested(uint256 indexed txId, uint256 amount);
    event StrategyWhitelisted(address indexed strategy, bool status);

    function setUp() public {
        vusdcRole = address(this); // test contract plays vUSDC for funding

        usdc  = new MockERC20();
        vault = new CapitalManager(IERC20(address(usdc)), owner, address(0));
        attestorAdapter = new BybitAttestor(
            address(usdc),
            address(vault),    // adapter's `vault` immutable
            attestor,
            owner
        );

        vault.setVusdc(vusdcRole);
        vault.setAgent(agent);

        // Fund the vault via the recordDeposit path (matches production flow).
        usdc.mint(vusdcRole, SEED);
        usdc.approve(address(vault), SEED);
        vault.recordDeposit(SEED);
    }

    // ─── Whitelisting ───────────────────────────────────────────────────────

    function test_Whitelist_AddsAdapter() public {
        assertFalse(vault.isWhitelisted(address(attestorAdapter)));

        vm.expectEmit(true, true, true, true);
        emit StrategyWhitelisted(address(attestorAdapter), true);
        vault.whitelistStrategy(address(attestorAdapter), true);

        assertTrue(vault.isWhitelisted(address(attestorAdapter)));
        assertEq(vault.whitelistedCount(), 1);
        assertEq(vault.whitelistedAt(0), address(attestorAdapter));
    }

    // ─── totalAssetsUsdc aggregation ────────────────────────────────────────

    function test_TotalAssetsUsdc_AggregatesAttestorValue() public {
        vault.whitelistStrategy(address(attestorAdapter), true);

        // Initially: vault holds SEED, attestor empty (no escrow, no attested).
        // valueInUsdc() returns 0 because lastAttestationTime == 0 AND
        // totalPendingDeposits == 0.
        assertEq(vault.totalAssetsUsdc(), SEED);

        // Push a confirmed deposit via the off-chain path: deposit() into
        // adapter escrow → attestor confirmDeposit(). After confirm, vault
        // USDC is still SEED - amount (escrow released to attestor), but
        // attested=amount → totalAssetsUsdc unchanged.
        uint256 amount = 100 * 1e6;

        // Approve and call deposit AS the vault (since deposit is onlyVault).
        vm.prank(address(vault));
        usdc.approve(address(attestorAdapter), amount);
        vm.prank(address(vault));
        attestorAdapter.deposit(amount);

        // After deposit: vault USDC -100, escrow +100, totalPending +100,
        // attested still 0. valueInUsdc = attested + pending = 100.
        // totalAssetsUsdc = (SEED - 100) + 100 = SEED.
        assertEq(vault.totalAssetsUsdc(), SEED);

        // Now attestor confirms with newBalance = amount (no growth yet).
        vm.prank(attestor);
        attestorAdapter.confirmDeposit(0, amount);

        // After confirm: escrow → attestor wallet (off-chain), pending=0,
        // attested=100. valueInUsdc = 100 + 0 = 100.
        // vault USDC = SEED - 100 (sent to attestor). totalAssetsUsdc = SEED.
        assertEq(vault.totalAssetsUsdc(), SEED);
        assertEq(attestorAdapter.valueInUsdc(), amount);
        assertEq(attestorAdapter.balance(), amount);
    }

    function test_TotalAssetsUsdc_StaleAttestation_RevertsFailLoud() public {
        vault.whitelistStrategy(address(attestorAdapter), true);

        // Seed an attested balance.
        uint256 amount = 100 * 1e6;
        vm.prank(address(vault));
        usdc.approve(address(attestorAdapter), amount);
        vm.prank(address(vault));
        attestorAdapter.deposit(amount);
        vm.prank(attestor);
        attestorAdapter.confirmDeposit(0, amount);

        // Step past the heartbeat.
        vm.warp(block.timestamp + attestorAdapter.HEARTBEAT() + 1);

        // valueInUsdc reverts, and totalAssetsUsdc must propagate it. This
        // is the fail-loud semantic CapitalManager documents — silently
        // undervaluing a position would dilute existing vUSDC holders.
        vm.expectRevert("attestation stale");
        vault.totalAssetsUsdc();
    }

    function test_TotalAssetsUsdc_DewhitelistResumesAfterStale() public {
        vault.whitelistStrategy(address(attestorAdapter), true);

        uint256 amount = 100 * 1e6;
        vm.prank(address(vault));
        usdc.approve(address(attestorAdapter), amount);
        vm.prank(address(vault));
        attestorAdapter.deposit(amount);
        vm.prank(attestor);
        attestorAdapter.confirmDeposit(0, amount);

        vm.warp(block.timestamp + attestorAdapter.HEARTBEAT() + 1);
        vm.expectRevert("attestation stale");
        vault.totalAssetsUsdc();

        // Operator removes the stale adapter — vault recovers without halting
        // the rest of CapitalManager. Matches the recovery procedure in
        // CapitalManager NatSpec ("de-whitelist the broken adapter").
        vault.whitelistStrategy(address(attestorAdapter), false);
        assertEq(vault.totalAssetsUsdc(), SEED - amount); // attestor was paid `amount`
    }

    // ─── executeAllocation Deposit ──────────────────────────────────────────

    function test_ExecuteAllocation_Deposit_RoutesToAttestor() public {
        vault.whitelistStrategy(address(attestorAdapter), true);

        uint256 amount = 200 * 1e6;
        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = CapitalManager.AllocationCall({
            adapter: address(attestorAdapter),
            kind:    CapitalManager.AllocationCallKind.Deposit,
            amount:  amount
        });

        // expectEmit on the BybitAttestor's DepositRequested before the call.
        vm.expectEmit(true, false, false, true, address(attestorAdapter));
        emit DepositRequested(0, amount);

        vm.prank(agent);
        vault.executeAllocation(DECISION_ID, calls, 0);

        // Escrow gained `amount`, vault lost `amount`, totalAssets unchanged.
        assertEq(usdc.balanceOf(address(attestorAdapter)), amount);
        assertEq(usdc.balanceOf(address(vault)), SEED - amount);
        assertEq(attestorAdapter.totalPendingDeposits(), amount);
        assertEq(attestorAdapter.pendingDeposits(0), amount);
        assertEq(attestorAdapter.valueInUsdc(), amount); // pending counts as value
        assertEq(vault.totalAssetsUsdc(), SEED);
    }

    // ─── executeAllocation Withdraw (async) ─────────────────────────────────

    function test_ExecuteAllocation_Withdraw_IsAsync() public {
        vault.whitelistStrategy(address(attestorAdapter), true);

        // Need an attested position first so withdraw makes business sense
        // (also so we have a non-stale valueInUsdc to read).
        uint256 amount = 100 * 1e6;
        vm.prank(address(vault));
        usdc.approve(address(attestorAdapter), amount);
        vm.prank(address(vault));
        attestorAdapter.deposit(amount);
        vm.prank(attestor);
        attestorAdapter.confirmDeposit(0, amount);

        // Snapshot for invariance.
        uint256 vaultUsdcBefore     = usdc.balanceOf(address(vault));
        uint256 totalAssetsBefore   = vault.totalAssetsUsdc();
        uint256 attestorValueBefore = attestorAdapter.valueInUsdc();

        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = CapitalManager.AllocationCall({
            adapter: address(attestorAdapter),
            kind:    CapitalManager.AllocationCallKind.Withdraw,
            amount:  amount
        });

        vm.expectEmit(true, false, false, true, address(attestorAdapter));
        emit WithdrawRequested(1, amount);

        vm.prank(agent);
        vault.executeAllocation(DECISION_ID, calls, 0);

        // The withdraw is async — nothing actually moves on-chain in this tx.
        // - vault USDC unchanged
        // - valueInUsdc unchanged (attested still includes the soon-to-leave funds)
        // - totalAssetsUsdc unchanged
        // - pendingWithdraws[txId=1] = amount (so attestor knows what to deliver)
        assertEq(usdc.balanceOf(address(vault)), vaultUsdcBefore);
        assertEq(attestorAdapter.valueInUsdc(), attestorValueBefore);
        assertEq(vault.totalAssetsUsdc(), totalAssetsBefore);
        assertEq(attestorAdapter.pendingWithdraws(1), amount);
    }

    function test_ExecuteAllocation_Withdraw_PassesPerCallLossGuard() public {
        // Per-call check: taPostCall >= taPreCall * (10000 - maxPerCallLossBps) / 10000
        // BybitAttestor.withdraw returns 0 and doesn't transfer — totalAssets
        // is unchanged → per-call check trivially passes even with the
        // default 100bps (1%) limit. This test pins that behavior so a future
        // re-interpretation of "withdraw returns 0" doesn't silently break it.
        vault.whitelistStrategy(address(attestorAdapter), true);

        uint256 amount = 100 * 1e6;
        vm.prank(address(vault));
        usdc.approve(address(attestorAdapter), amount);
        vm.prank(address(vault));
        attestorAdapter.deposit(amount);
        vm.prank(attestor);
        attestorAdapter.confirmDeposit(0, amount);

        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = CapitalManager.AllocationCall({
            adapter: address(attestorAdapter),
            kind:    CapitalManager.AllocationCallKind.Withdraw,
            amount:  amount
        });

        // Tighten the per-call cap to 0bps to prove no value moved.
        vault.setMaxPerCallLossBps(0);
        vault.setMaxSlippageBps(0);

        vm.prank(agent);
        vault.executeAllocation(DECISION_ID, calls, 0); // does not revert
    }

    // ─── Full deposit lifecycle through executeAllocation ───────────────────

    function test_FullDepositLifecycle_TotalAssetsSteady() public {
        vault.whitelistStrategy(address(attestorAdapter), true);

        uint256 amount = 250 * 1e6;
        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = CapitalManager.AllocationCall({
            adapter: address(attestorAdapter),
            kind:    CapitalManager.AllocationCallKind.Deposit,
            amount:  amount
        });

        uint256 ta0 = vault.totalAssetsUsdc();
        vm.prank(agent);
        vault.executeAllocation(DECISION_ID, calls, 0);
        uint256 ta1 = vault.totalAssetsUsdc();
        assertEq(ta1, ta0, "totalAssets must be conserved across Deposit");

        // Attestor settles off-chain (skipped here) and pushes confirmDeposit.
        vm.prank(attestor);
        attestorAdapter.confirmDeposit(0, amount);

        uint256 ta2 = vault.totalAssetsUsdc();
        assertEq(ta2, ta0, "totalAssets must be conserved across confirmDeposit");

        // Subsequent steady-state updateBalance push tracks yield growth.
        // Use +5% (within +10% bound).
        uint256 grown = amount + (amount * 5) / 100;
        vm.prank(attestor);
        attestorAdapter.updateBalance(grown);

        // totalAssets now reflects the yield: vault USDC + grown attested.
        // vault USDC = SEED - amount, attestor.valueInUsdc = grown.
        assertEq(vault.totalAssetsUsdc(), (SEED - amount) + grown);
    }
}
