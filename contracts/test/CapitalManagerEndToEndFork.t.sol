// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test, console} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {CapitalManager} from "../src/CapitalManager.sol";
import {AaveV3UsdcAdapter} from "../src/adapters/AaveV3UsdcAdapter.sol";
import {AaveV3WethAdapter} from "../src/adapters/AaveV3WethAdapter.sol";
import {IAaveOracle, IChainlinkAggregator} from "../src/adapters/interfaces/IAaveOracle.sol";

/// @notice End-to-end fork test on Mantle mainnet — exercises CapitalManager
/// under the post-pivot vUSDC architecture:
///   - the test contract plays the vUSDC role (set via setVusdc),
///   - users go through `recordDeposit` / `recordWithdraw`,
///   - agent allocates USDC into the live Aave V3 USDC market,
///   - WethAdapter is whitelisted and exercised by `deal`-ing WETH directly
///     into the vault, simulating the post-swap-leg state of a future
///     WETH→USDC routing adapter (verified missing on Mantle, 2026-05-19).
///
/// Run: forge test --match-contract CapitalManagerEndToEndFork -vv
contract CapitalManagerEndToEndForkTest is Test {
    // Mantle Mainnet — verified addresses (notes/addresses.md).
    address constant AAVE_POOL   = 0x458F293454fE0d67EC0655f3672301301DD51422;
    address constant AAVE_ORACLE = 0x47a063CfDa980532267970d478EC340C0F80E8df;
    address constant USDC        = 0x09Bc4E0D864854c6aFB6eB9A9cdF58aC190D0dF9;
    address constant WETH        = 0xdEAddEaDdeadDEadDEADDEAddEADDEAddead1111;
    address constant aUSDC       = 0xcb8164415274515867ec43CbD284ab5d6d2b304F;
    address constant aWETH       = 0xeAC30Ed8609F564aE65C809C4bf42dB2fF426D2C;

    CapitalManager    vault;
    AaveV3UsdcAdapter usdcAdapter;
    AaveV3WethAdapter wethAdapter;

    address owner     = address(this);
    address agent     = address(0xCAFE);
    address vusdcRole = address(this); // test contract = vUSDC for recordDeposit/Withdraw
    address user      = address(0xBEEF);

    function setUp() public {
        string memory rpc = vm.envOr("MANTLE_RPC_URL", string("https://rpc.mantle.xyz"));
        vm.createSelectFork(rpc);

        vault       = new CapitalManager(IERC20(USDC), owner, address(0));
        usdcAdapter = new AaveV3UsdcAdapter(AAVE_POOL, USDC, aUSDC, address(vault), owner);
        wethAdapter = new AaveV3WethAdapter(AAVE_POOL, AAVE_ORACLE, WETH, aWETH, USDC, address(vault), owner);

        vault.whitelistStrategy(address(usdcAdapter), true);
        vault.whitelistStrategy(address(wethAdapter), true);
        vault.setAgent(agent);
        vault.setVusdc(vusdcRole);

        // Loose slippage caps so pre/post TA jumps (caused by `deal`-positioning
        // non-cash assets) don't fire the global cap. Slippage logic is covered
        // separately in CapitalManagerSlippage.t.sol.
        vault.setMaxSlippageBps(10000);
        vault.setMaxPerCallLossBps(10000);
    }

    /// @dev Simulate a vUSDC mint by funding the vusdc role with USDC and
    /// calling recordDeposit. The vusdc role is the test contract itself.
    function _recordDeposit(uint256 amt) internal {
        deal(USDC, vusdcRole, amt);
        IERC20(USDC).approve(address(vault), amt);
        vault.recordDeposit(amt);
    }

    // ═══ full lifecycle: deposit → allocate → deallocate → withdraw ══════════

    /// @notice Full user lifecycle on live Mantle Aave V3 USDC market. Confirms
    /// recordDeposit → allocate → deallocate → recordWithdraw round-trips with
    /// real protocol-side rounding (1-wei aToken minting losses tolerated).
    function test_FullCycle_RecordDepositAllocateDeallocateWithdraw_LiveMantle() public {
        _recordDeposit(5_000e6);
        assertEq(IERC20(USDC).balanceOf(address(vault)), 5_000e6, "post-deposit cash");
        assertEq(vault.totalAssetsUsdc(), 5_000e6, "TA after deposit");

        // Allocate 3000 USDC into Aave.
        CapitalManager.AllocationCall[] memory in_ = new CapitalManager.AllocationCall[](1);
        in_[0] = CapitalManager.AllocationCall(address(usdcAdapter), CapitalManager.AllocationCallKind.Deposit, 3_000e6);
        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(1)), in_, 0);

        // Aave can shave 1 wei on aToken minting — TA may dip by 1 wei.
        assertApproxEqAbs(vault.totalAssetsUsdc(), 5_000e6, 1, "TA after allocate");

        // Deallocate the full adapter position.
        CapitalManager.AllocationCall[] memory out_ = new CapitalManager.AllocationCall[](1);
        out_[0] = CapitalManager.AllocationCall(
            address(usdcAdapter),
            CapitalManager.AllocationCallKind.Withdraw,
            usdcAdapter.balance()
        );
        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(2)), out_, 0);

        // Vault should hold back ~5000 USDC in cash (within 2-wei rounding window).
        assertApproxEqAbs(IERC20(USDC).balanceOf(address(vault)), 5_000e6, 2, "post-deallocate cash");

        // vUSDC redeems all USDC back to the user.
        uint256 cashAvailable = IERC20(USDC).balanceOf(address(vault));
        vault.recordWithdraw(cashAvailable, user);
        assertApproxEqAbs(IERC20(USDC).balanceOf(user), 5_000e6, 2, "user USDC back");
    }

    // ═══ multi-strategy atomic allocation ═════════════════════════════════════

    /// @notice Single `executeAllocation` routes USDC into the Aave USDC market
    /// AND pre-positioned WETH (deal-ed; standing in for a future swap leg)
    /// into the Aave WETH market — in one atomic tx. Asserts each adapter
    /// holds a non-trivial position and totalAssetsUsdc sums coherently.
    function test_MultiStrategy_UsdcPlusPrePositionedWeth_LiveMantle() public {
        _recordDeposit(10_000e6); // 10 000 USDC

        // Pre-position 2 WETH directly into the vault (simulates post-swap state).
        deal(WETH, address(vault), 2 ether);

        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](2);
        calls[0] = CapitalManager.AllocationCall(address(usdcAdapter), CapitalManager.AllocationCallKind.Deposit, 5_000e6);
        calls[1] = CapitalManager.AllocationCall(address(wethAdapter), CapitalManager.AllocationCallKind.Deposit, 2 ether);

        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(1)), calls, 0);

        // Each adapter holds a non-trivial position.
        assertGt(usdcAdapter.balance(), 0, "USDC adapter empty");
        assertGt(wethAdapter.balance(), 0, "WETH adapter empty");

        // valueInUsdc on each. USDC adapter is trivially aUSDC.balanceOf.
        assertApproxEqAbs(usdcAdapter.valueInUsdc(), 5_000e6, 1, "USDC adapter value drift");

        // WETH adapter: priced via Aave Oracle.
        uint256 wethValue = wethAdapter.valueInUsdc();
        // 2 WETH @ $1500-$5000 → 3 000 – 10 000 USDC.
        assertGt(wethValue, 3_000e6, "WETH value implausibly low");
        assertLt(wethValue, 10_000e6, "WETH value implausibly high");
    }

    /// @notice `totalAssetsUsdc()` equals vault free USDC + each adapter's
    /// `valueInUsdc()` after a multi-strategy allocation. Independently
    /// recomputes the expected sum from on-chain primitives.
    function test_TotalAssetsConsistency_AfterMultiStrategy_LiveMantle() public {
        _recordDeposit(10_000e6);
        deal(WETH, address(vault), 1 ether);

        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](2);
        calls[0] = CapitalManager.AllocationCall(address(usdcAdapter), CapitalManager.AllocationCallKind.Deposit, 4_000e6);
        calls[1] = CapitalManager.AllocationCall(address(wethAdapter), CapitalManager.AllocationCallKind.Deposit, 1 ether);

        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(2)), calls, 0);

        uint256 freeUsdc = IERC20(USDC).balanceOf(address(vault));
        uint256 expected = freeUsdc + usdcAdapter.valueInUsdc() + wethAdapter.valueInUsdc();

        assertEq(vault.totalAssetsUsdc(), expected, "totalAssetsUsdc != free + sum(value)");
    }

    /// @notice Mirror the WethAdapter formula independently using the same
    /// Aave Oracle path. Catches decimal-math regressions inside the adapter.
    function test_WethAdapter_MirrorOracleFormula_LiveMantle() public {
        deal(WETH, address(vault), 1 ether);

        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = CapitalManager.AllocationCall(address(wethAdapter), CapitalManager.AllocationCallKind.Deposit, 1 ether);
        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(3)), calls, 0);

        IAaveOracle o = IAaveOracle(AAVE_ORACLE);
        uint256 wethPrice = uint256(IChainlinkAggregator(o.getSourceOfAsset(WETH)).latestAnswer());
        uint256 usdcPrice = uint256(IChainlinkAggregator(o.getSourceOfAsset(USDC)).latestAnswer());

        uint256 aBal = IERC20(aWETH).balanceOf(address(wethAdapter));
        uint256 expectedUsdc = (aBal * wethPrice * 1e6) / (usdcPrice * 1e18);

        assertEq(wethAdapter.valueInUsdc(), expectedUsdc, "valueInUsdc != mirror formula");
    }

    // ═══ partial revert atomicity ═════════════════════════════════════════════

    /// @notice On a partial-revert in a multi-call batch, the entire tx must
    /// roll back — the prior successful call must NOT have persisted state.
    /// Triggered here by attempting to withdraw from a zero-balance adapter.
    function test_PartialRevert_RollsBackOnFork_LiveMantle() public {
        _recordDeposit(2_000e6);
        uint256 vaultUsdcBefore = IERC20(USDC).balanceOf(address(vault));

        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](2);
        // call[0]: legitimate deposit.
        calls[0] = CapitalManager.AllocationCall(address(usdcAdapter), CapitalManager.AllocationCallKind.Deposit, 1_000e6);
        // call[1]: withdraw 100 WETH from a WethAdapter that holds 0 aWETH → Aave reverts.
        calls[1] = CapitalManager.AllocationCall(address(wethAdapter), CapitalManager.AllocationCallKind.Withdraw, 100 ether);

        vm.expectRevert();
        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(99)), calls, 0);

        // call[0] must NOT have persisted.
        assertEq(IERC20(USDC).balanceOf(address(vault)), vaultUsdcBefore, "free USDC drifted on revert");
        assertEq(usdcAdapter.balance(), 0, "USDC adapter took funds despite revert");
    }
}
