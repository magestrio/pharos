// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test, console} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {CapitalManager} from "../src/CapitalManager.sol";
import {AaveV3WethAdapter}  from "../src/adapters/AaveV3WethAdapter.sol";
import {AaveV3UsdcAdapter}  from "../src/adapters/AaveV3UsdcAdapter.sol";
import {AaveV3SusdeAdapter} from "../src/adapters/AaveV3SusdeAdapter.sol";

/// @notice End-to-end fork test on Mantle mainnet — exercises CapitalManager's
/// multi-call execution against real Aave V3 with three live strategies
/// (WETH, USDC, sUSDe) in a single `executeAllocation` transaction.
///
/// Caveat — WETH↔stable swap leg:
///   Vault's base asset is WETH, but Merchant Moe LB has no WETH↔USDC /
///   WETH↔USDe pair (verified 2026-05-19, see `notes/addresses.md` "Открытая
///   проблема"). A production cycle therefore requires either a separate
///   swap adapter routing through Agni / iZiSwap / KTX, or an aggregator
///   (0x, 1inch) — neither is in this repo yet. This test simulates the
///   post-swap state by `deal`-ing USDC and sUSDe directly into the vault
///   and verifies that `executeAllocation` correctly routes pre-positioned
///   funds to multiple adapters atomically. Real WETH→USDC/sUSDe routing
///   lands in a future subtask.
///
/// Run: forge test --match-contract CapitalManagerEndToEndFork -vv
contract CapitalManagerEndToEndForkTest is Test {
    // Mantle Mainnet — verified addresses (notes/addresses.md).
    address constant AAVE_POOL   = 0x458F293454fE0d67EC0655f3672301301DD51422;
    address constant AAVE_ORACLE = 0x47a063CfDa980532267970d478EC340C0F80E8df;
    address constant WETH        = 0xdEAddEaDdeadDEadDEADDEAddEADDEAddead1111;
    address constant USDC        = 0x09Bc4E0D864854c6aFB6eB9A9cdF58aC190D0dF9;
    address constant sUSDe       = 0x211Cc4DD073734dA055fbF44a2b4667d5E5fE5d2;
    address constant aWETH       = 0xeAC30Ed8609F564aE65C809C4bf42dB2fF426D2C;
    address constant aUSDC       = 0xcb8164415274515867ec43CbD284ab5d6d2b304F;
    address constant asUSDe      = 0xaf972F332FF79bd32A6CB6B54f903eA0F9b16C2a;

    CapitalManager vault;
    AaveV3WethAdapter  wethAdapter;
    AaveV3UsdcAdapter  usdcAdapter;
    AaveV3SusdeAdapter susdeAdapter;

    address owner = address(this);
    address agent = address(0xCAFE);
    address user  = address(0xBEEF);

    function setUp() public {
        string memory rpc = vm.envOr("MANTLE_RPC_URL", string("https://rpc.mantle.xyz"));
        vm.createSelectFork(rpc);

        vault = new CapitalManager(IERC20(WETH), owner, "CapitalManager WETH", "v8004-WETH", address(0));

        wethAdapter  = new AaveV3WethAdapter (AAVE_POOL, WETH, aWETH,  address(vault), owner);
        usdcAdapter  = new AaveV3UsdcAdapter (AAVE_POOL, AAVE_ORACLE, USDC,  aUSDC,  WETH, address(vault), owner);
        susdeAdapter = new AaveV3SusdeAdapter(AAVE_POOL, AAVE_ORACLE, sUSDe, asUSDe, WETH, address(vault), owner);

        vault.whitelistStrategy(address(wethAdapter),  true);
        vault.whitelistStrategy(address(usdcAdapter),  true);
        vault.whitelistStrategy(address(susdeAdapter), true);
        vault.setAgent(agent);

        // Loose slippage caps — pre/post TA changes drastically when seeded
        // non-base assets first appear in totalAssets() via adapter deposits.
        // Slippage-gate behaviour is covered by CapitalManagerSlippage.t.sol.
        vault.setMaxSlippageBps(10000);
        vault.setMaxPerCallLossBps(10000);
    }

    function _userDepositWeth(uint256 amount) internal {
        deal(WETH, user, amount);
        vm.startPrank(user);
        IERC20(WETH).approve(address(vault), amount);
        vault.deposit(amount, user);
        vm.stopPrank();
    }

    // ═══ multi-strategy atomic allocation ═════════════════════════════════════

    /// @notice Single `executeAllocation` routes pre-positioned WETH + USDC +
    /// sUSDe into three Aave adapters in one tx. Verifies atomicity and that
    /// each adapter holds a positive position priced via real Aave Oracle.
    function test_MultiStrategy_ThreeAdapters_AtomicAllocation_LiveMantle() public {
        // User deposits 10 WETH via ERC-4626 standard path.
        _userDepositWeth(10 ether);

        // Pre-position non-base assets directly into the vault (simulates the
        // post-swap state of a hypothetical WETH→USDC/sUSDe swap leg).
        deal(USDC, address(vault), 5_000e6);    // 5 000 USDC
        deal(sUSDe, address(vault), 2_000e18);  // 2 000 sUSDe

        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](3);
        calls[0] = CapitalManager.AllocationCall({
            adapter: address(wethAdapter),
            kind: CapitalManager.AllocationCallKind.Deposit,
            amount: 4 ether
        });
        calls[1] = CapitalManager.AllocationCall({
            adapter: address(usdcAdapter),
            kind: CapitalManager.AllocationCallKind.Deposit,
            amount: 5_000e6
        });
        calls[2] = CapitalManager.AllocationCall({
            adapter: address(susdeAdapter),
            kind: CapitalManager.AllocationCallKind.Deposit,
            amount: 2_000e18
        });

        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(1)), calls, 0);

        // Each adapter should hold a non-trivial position.
        assertGt(wethAdapter.balance(),  0, "WETH adapter empty");
        assertGt(usdcAdapter.balance(),  0, "USDC adapter empty");
        assertGt(susdeAdapter.balance(), 0, "sUSDe adapter empty");

        // valueInBaseAsset returns WETH-denominated (18 decimals). Each non-zero.
        assertGt(wethAdapter.valueInBaseAsset(),  0, "WETH valueInBaseAsset = 0");
        assertGt(usdcAdapter.valueInBaseAsset(),  0, "USDC valueInBaseAsset = 0");
        assertGt(susdeAdapter.valueInBaseAsset(), 0, "sUSDe valueInBaseAsset = 0");

        // WETH adapter holds 4 WETH (aWETH 1:1 with WETH).
        assertApproxEqAbs(wethAdapter.valueInBaseAsset(), 4 ether, 1, "WETH value drift");
    }

    /// @notice `totalAssets()` matches the sum of every adapter's WETH-priced
    /// value plus the vault's free WETH balance after a multi-strategy
    /// allocation. Confirms the accounting identity over real oracle reads.
    function test_TotalAssetsConsistency_AfterMultiStrategy_LiveMantle() public {
        _userDepositWeth(10 ether);
        deal(USDC,  address(vault), 5_000e6);
        deal(sUSDe, address(vault), 2_000e18);

        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](3);
        calls[0] = CapitalManager.AllocationCall(address(wethAdapter),  CapitalManager.AllocationCallKind.Deposit, 4 ether);
        calls[1] = CapitalManager.AllocationCall(address(usdcAdapter),  CapitalManager.AllocationCallKind.Deposit, 5_000e6);
        calls[2] = CapitalManager.AllocationCall(address(susdeAdapter), CapitalManager.AllocationCallKind.Deposit, 2_000e18);

        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(2)), calls, 0);

        uint256 free = IERC20(WETH).balanceOf(address(vault));
        uint256 sumValue =
            wethAdapter.valueInBaseAsset()  +
            usdcAdapter.valueInBaseAsset()  +
            susdeAdapter.valueInBaseAsset();

        assertEq(vault.totalAssets(), free + sumValue, "totalAssets mismatch");
    }

    // ═══ full lifecycle: deposit → allocate → deallocate → withdraw ══════════

    /// @notice Full user lifecycle on live Mantle Aave V3. Confirms that
    /// allocate → deallocate → ERC-4626 withdraw round-trips correctly with
    /// real protocol-side rounding (1-wei aToken minting losses tolerated).
    function test_FullCycle_DepositAllocateDeallocateWithdraw_LiveMantle() public {
        _userDepositWeth(5 ether);
        uint256 sharesBefore = vault.balanceOf(user);
        assertEq(sharesBefore, 5 ether, "first deposit 1:1");

        // Allocate 3 WETH into Aave.
        CapitalManager.AllocationCall[] memory in_ = new CapitalManager.AllocationCall[](1);
        in_[0] = CapitalManager.AllocationCall(address(wethAdapter), CapitalManager.AllocationCallKind.Deposit, 3 ether);
        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(1)), in_, 0);

        // Deallocate the position back.
        CapitalManager.AllocationCall[] memory out_ = new CapitalManager.AllocationCall[](1);
        out_[0] = CapitalManager.AllocationCall(address(wethAdapter), CapitalManager.AllocationCallKind.Withdraw, wethAdapter.balance());
        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(2)), out_, 0);

        // Vault should hold back ~5 WETH (Aave can shave 1 wei on minting).
        assertApproxEqAbs(IERC20(WETH).balanceOf(address(vault)), 5 ether, 2, "vault free WETH");

        // User redeems all shares.
        vm.prank(user);
        vault.redeem(sharesBefore, user, user);
        assertApproxEqAbs(IERC20(WETH).balanceOf(user), 5 ether, 2, "user WETH back");
    }

    /// @notice Partial-revert atomicity on real Aave fork — when one call in
    /// a batch reverts (we trigger this by exceeding the adapter's balance on
    /// a Withdraw), the prior successful call MUST be rolled back too.
    function test_PartialRevert_RollsBackOnFork_LiveMantle() public {
        _userDepositWeth(2 ether);
        uint256 vaultWethBefore = IERC20(WETH).balanceOf(address(vault));

        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](2);
        // call[0]: legitimate deposit.
        calls[0] = CapitalManager.AllocationCall(address(wethAdapter), CapitalManager.AllocationCallKind.Deposit, 1 ether);
        // call[1]: withdraw 100 WETH from an empty adapter (USDC adapter has 0 aUSDC) → Aave reverts.
        calls[1] = CapitalManager.AllocationCall(address(usdcAdapter), CapitalManager.AllocationCallKind.Withdraw, 100e6);

        vm.expectRevert();
        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(99)), calls, 0);

        // call[0] must NOT have persisted.
        assertEq(IERC20(WETH).balanceOf(address(vault)), vaultWethBefore, "free WETH drifted on revert");
        assertEq(wethAdapter.balance(), 0, "WETH adapter took funds despite revert");
    }
}
