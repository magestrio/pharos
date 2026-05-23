// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Vault8004} from "../src/Vault8004.sol";
import {AaveV3WethAdapter} from "../src/adapters/AaveV3WethAdapter.sol";

/// @notice Fork-test for Vault8004.totalAssets() with real Aave V3 WETH adapter
/// on Mantle mainnet. Verifies that totalAssets() correctly sums vault free
/// balance + adapter valueInBaseAsset().
/// Run: forge test --match-contract TotalAssetsFork -vv
contract TotalAssetsForkTest is Test {
    address constant AAVE_POOL = 0x458F293454fE0d67EC0655f3672301301DD51422;
    address constant WETH      = 0xdEAddEaDdeadDEadDEADDEAddEADDEAddead1111;
    address constant aWETH     = 0xeAC30Ed8609F564aE65C809C4bf42dB2fF426D2C;

    Vault8004 vault;
    AaveV3WethAdapter adapter;

    address owner = address(this);
    address agent = address(0xCAFE);
    address user  = address(0xBEEF);

    function setUp() public {
        string memory rpc = vm.envOr("MANTLE_RPC_URL", string("https://rpc.mantle.xyz"));
        vm.createSelectFork(rpc);

        // Sequencer feed disabled (address(0)) — Mantle feed address not yet verified.
        vault = new Vault8004(IERC20(WETH), owner, "Vault8004 WETH", "v8004-WETH", address(0));
        adapter = new AaveV3WethAdapter(AAVE_POOL, WETH, aWETH, address(vault), owner);

        vault.whitelistStrategy(address(adapter), true);
        vault.setAgent(agent);
    }

    function test_TotalAssets_EmptyVault() public view {
        assertEq(vault.totalAssets(), 0, "empty vault should report zero");
    }

    function test_TotalAssets_OnlyFreeBalance() public {
        deal(WETH, address(vault), 1e18);
        assertEq(vault.totalAssets(), 1e18, "free balance only");
    }

    function test_TotalAssets_SumsFreeAndAdapter() public {
        // User deposits 1 WETH into the vault.
        deal(WETH, user, 1e18);
        vm.startPrank(user);
        IERC20(WETH).approve(address(vault), 1e18);
        vault.deposit(1e18, user);
        vm.stopPrank();

        // Agent allocates half to the Aave adapter.
        Vault8004.AllocationCall[] memory calls = new Vault8004.AllocationCall[](1);
        calls[0] = Vault8004.AllocationCall({
            adapter: address(adapter),
            kind: Vault8004.AllocationCallKind.Deposit,
            amount: 0.5e18
        });

        vm.prank(agent);
        vault.executeAllocation(bytes32(uint256(1)), calls, 0);

        // After allocation: 0.5 WETH free + 0.5 WETH in adapter (aWETH ~1:1).
        // Aave can round aToken minting down by 1 wei, hence the tolerance.
        assertApproxEqAbs(vault.totalAssets(), 1e18, 1, "free + adapter sum");
    }

    function test_TotalAssets_Bricked_WhenAdapterRevertsValueInBaseAsset() public {
        // Whitelist a stub adapter whose valueInBaseAsset() reverts (e.g. AaveV3UsdcAdapter
        // before .4 wires its oracle). totalAssets() MUST revert — fail-loud guarantee.
        // We simulate by whitelisting an arbitrary address that has no code; the staticcall
        // will revert with an empty return data, which propagates up.
        address phantom = address(0xDEAD1234);
        vault.whitelistStrategy(phantom, true);

        vm.expectRevert();
        vault.totalAssets();
    }
}
