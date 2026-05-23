// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {CapitalManager} from "../src/CapitalManager.sol";
import {AaveV3UsdcAdapter} from "../src/adapters/AaveV3UsdcAdapter.sol";

/// @notice Fork-test for CapitalManager.totalAssetsUsdc() with real Aave V3 USDC
/// adapter on Mantle mainnet. Verifies that the view correctly sums vault free
/// USDC balance + adapter valueInUsdc().
/// Run: forge test --match-contract TotalAssetsFork -vv
contract TotalAssetsForkTest is Test {
    address constant AAVE_POOL = 0x458F293454fE0d67EC0655f3672301301DD51422;
    address constant USDC      = 0x09Bc4E0D864854c6aFB6eB9A9cdF58aC190D0dF9;
    address constant aUSDC     = 0xcb8164415274515867ec43CbD284ab5d6d2b304F;

    CapitalManager    vault;
    AaveV3UsdcAdapter adapter;

    address owner     = address(this);
    address agent     = address(0xCAFE);
    address vusdcRole = address(this);

    function setUp() public {
        string memory rpc = vm.envOr("MANTLE_RPC_URL", string("https://rpc.mantle.xyz"));
        vm.createSelectFork(rpc);

        // Sequencer feed disabled (address(0)) — Mantle feed address not yet verified.
        vault   = new CapitalManager(IERC20(USDC), owner, address(0));
        adapter = new AaveV3UsdcAdapter(AAVE_POOL, USDC, aUSDC, address(vault), owner);

        vault.whitelistStrategy(address(adapter), true);
        vault.setAgent(agent);
        vault.setVusdc(vusdcRole);
    }

    function test_TotalAssets_EmptyVault() public view {
        assertEq(vault.totalAssetsUsdc(), 0, "empty vault should report zero");
    }

    function test_TotalAssets_OnlyFreeBalance() public {
        deal(USDC, address(vault), 1000e6);
        assertEq(vault.totalAssetsUsdc(), 1000e6, "free balance only");
    }

    function test_TotalAssets_SumsFreeAndAdapter() public {
        // Test contract plays the vUSDC role: fund self, approve, recordDeposit.
        deal(USDC, vusdcRole, 1000e6);
        IERC20(USDC).approve(address(vault), 1000e6);
        vault.recordDeposit(1000e6);

        // Agent allocates half (500 USDC) to the Aave adapter.
        CapitalManager.AllocationCall[] memory calls = new CapitalManager.AllocationCall[](1);
        calls[0] = CapitalManager.AllocationCall({
            adapter: address(adapter),
            kind: CapitalManager.AllocationCallKind.Deposit,
            amount: 500e6
        });

        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(1)), calls, 0);

        // After allocation: 500 USDC free + 500 USDC in adapter (aUSDC ~1:1).
        // Aave can round aToken minting down by 1 wei, hence the tolerance.
        assertApproxEqAbs(vault.totalAssetsUsdc(), 1000e6, 1, "free + adapter sum");
    }

    function test_TotalAssets_Bricked_WhenAdapterRevertsValueInUsdc() public {
        // Whitelist a phantom adapter whose valueInUsdc() reverts (no code at address).
        // totalAssetsUsdc() MUST revert — fail-loud guarantee.
        address phantom = address(0xDEAD1234);
        vault.whitelistStrategy(phantom, true);

        vm.expectRevert();
        vault.totalAssetsUsdc();
    }
}
