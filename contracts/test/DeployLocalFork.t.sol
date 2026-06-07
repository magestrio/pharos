// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {CapitalManager} from "../src/CapitalManager.sol";
import {ICapitalManager} from "../src/interfaces/ICapitalManager.sol";
import {VUSDC} from "../src/VUSDC.sol";
import {DecisionLog} from "../src/DecisionLog.sol";
import {ReputationOracle} from "../src/ReputationOracle.sol";
import {AaveV3WethAdapter} from "../src/adapters/AaveV3WethAdapter.sol";
import {AaveV3UsdcAdapter} from "../src/adapters/AaveV3UsdcAdapter.sol";
import {BybitAttestor} from "../src/adapters/BybitAttestor.sol";

/// @notice Integration test mirroring `script/DeployLocal.s.sol`. Catches
/// regressions in the local-deploy path *before* a real `pnpm start` run
/// errors out half-way through.
///
/// Why a fork test, not pure unit: the adapters (Aave, Bybit) reference
/// canonical Mantle mainnet addresses in their constructors. Pure-anvil
/// (no fork) deploy would fail at first `aPool.supply()` call. The fork
/// is also what `pnpm start` does, so the test matches reality.
///
/// Run:
///   MANTLE_RPC_URL=https://rpc.mantle.xyz forge test --match-contract DeployLocalFork -vv
contract DeployLocalForkTest is Test {
    // Mirror DeployLocal.s.sol constants — single source of truth would
    // be a shared library, deferred (one tiny duplication beats a refactor).
    address constant WETH  = 0xdEAddEaDdeadDEadDEADDEAddEADDEAddead1111;
    address constant USDC  = 0x09Bc4E0D864854c6aFB6eB9A9cdF58aC190D0dF9;
    address constant AAVE_POOL   = 0x458F293454fE0d67EC0655f3672301301DD51422;
    address constant AAVE_ORACLE = 0x47a063CfDa980532267970d478EC340C0F80E8df;
    address constant AWETH       = 0xeAC30Ed8609F564aE65C809C4bf42dB2fF426D2C;
    address constant AUSDC       = 0xcb8164415274515867ec43CbD284ab5d6d2b304F;
    address constant REPUTATION_REGISTRY_8004 = 0x8004BAa17C55a88189AE136b182e5fdA19dE9b63;

    // Anvil dev accounts — same ones scripts/lib/anvil.sh wires.
    address constant DEPLOYER = 0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266;
    address constant AGENT    = 0x70997970C51812dc3A010C7d01b50e0d17dc79C8;
    address constant ATTESTOR = 0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC;
    uint256 constant AGENT_ID = 1;

    CapitalManager vault;
    VUSDC vusdc;
    DecisionLog decisionLog;
    ReputationOracle oracle;
    AaveV3WethAdapter wethAdapter;
    AaveV3UsdcAdapter usdcAdapter;
    BybitAttestor bybitAttestor;

    function setUp() public {
        string memory rpc = vm.envOr("MANTLE_RPC_URL", string("https://rpc.mantle.xyz"));
        vm.createSelectFork(rpc);

        // Same sequence as DeployLocal.run() — keep in lockstep.
        vm.startPrank(DEPLOYER);

        vault = new CapitalManager(IERC20(USDC), DEPLOYER, address(0));
        vusdc = new VUSDC(ICapitalManager(address(vault)));
        decisionLog = new DecisionLog(DEPLOYER);
        oracle = new ReputationOracle(
            address(vusdc), REPUTATION_REGISTRY_8004, AGENT_ID
        );

        wethAdapter = new AaveV3WethAdapter(
            AAVE_POOL, AAVE_ORACLE, WETH, AWETH, USDC, address(vault), DEPLOYER
        );
        usdcAdapter = new AaveV3UsdcAdapter(
            AAVE_POOL, USDC, AUSDC, address(vault), DEPLOYER
        );
        bybitAttestor = new BybitAttestor(
            USDC, address(vault), ATTESTOR, DEPLOYER
        );

        vault.whitelistStrategy(address(wethAdapter), true);
        vault.whitelistStrategy(address(usdcAdapter), true);
        vault.whitelistStrategy(address(bybitAttestor), true);

        vault.setAgent(AGENT);
        decisionLog.setAgent(AGENT);
        vault.setVusdc(address(vusdc));

        vm.stopPrank();
    }

    function test_AllContractsHaveCode() public view {
        assertGt(address(vault).code.length,        0, "CapitalManager has no code");
        assertGt(address(vusdc).code.length,        0, "VUSDC has no code");
        assertGt(address(decisionLog).code.length,  0, "DecisionLog has no code");
        assertGt(address(oracle).code.length,       0, "ReputationOracle has no code");
        assertGt(address(wethAdapter).code.length,  0, "AaveV3WethAdapter has no code");
        assertGt(address(usdcAdapter).code.length,  0, "AaveV3UsdcAdapter has no code");
        assertGt(address(bybitAttestor).code.length,0, "BybitAttestor has no code");
    }

    function test_CapitalManagerWired() public view {
        assertEq(vault.owner(), DEPLOYER,     "CM owner != deployer");
        assertEq(vault.agent(), AGENT,        "CM agent != anvil#1");
        assertEq(vault.vusdc(), address(vusdc), "CM.vusdc not set");
        assertEq(address(vault.usdc()), USDC, "CM.usdc != Mantle USDC");
    }

    function test_VUSDCBackedByCapitalManager() public view {
        assertEq(address(vusdc.capitalManager()), address(vault), "VUSDC.capitalManager mismatch");
        assertEq(vusdc.totalSupply(), 0,        "fresh deploy must have 0 supply");
        // Sanity on metadata so the UI / explorer surface remains stable.
        assertEq(keccak256(bytes(vusdc.name())),   keccak256(bytes("Vault USDC")));
        assertEq(keccak256(bytes(vusdc.symbol())), keccak256(bytes("vUSDC")));
    }

    function test_DecisionLogAgentSet() public view {
        assertEq(decisionLog.owner(), DEPLOYER, "DecisionLog owner != deployer");
        assertEq(decisionLog.agent(), AGENT,    "DecisionLog agent != anvil#1");
    }

    function test_ReputationOracleWired() public view {
        assertEq(address(oracle.vault()),    address(vusdc),                "Oracle.vault != VUSDC");
        assertEq(address(oracle.registry()), REPUTATION_REGISTRY_8004,      "Oracle.registry != canonical 8004");
        assertEq(oracle.agentId(),           AGENT_ID,                      "Oracle.agentId mismatch");
    }

    function test_AdaptersWhitelisted() public view {
        assertTrue(vault.isWhitelisted(address(wethAdapter)),   "Weth adapter not whitelisted");
        assertTrue(vault.isWhitelisted(address(usdcAdapter)),   "USDC adapter not whitelisted");
        assertTrue(vault.isWhitelisted(address(bybitAttestor)), "Bybit attestor not whitelisted");
    }

    function test_SetVusdcIsOneShot() public {
        // `vault.setVusdc` already fired in setUp; calling again must
        // revert (immutable-after-set invariant). Guards a deploy-script
        // regression where someone wires vUSDC twice.
        vm.prank(DEPLOYER);
        vm.expectRevert(bytes("vusdc set"));
        vault.setVusdc(address(0xdEAD));
    }
}
