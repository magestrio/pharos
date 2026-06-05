// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {CapitalManager} from "../src/CapitalManager.sol";
import {ICapitalManager} from "../src/interfaces/ICapitalManager.sol";
import {VUSDC} from "../src/VUSDC.sol";
import {DecisionLog} from "../src/DecisionLog.sol";
import {ReputationOracle} from "../src/ReputationOracle.sol";
import {MerchantMoeAdapter} from "../src/adapters/MerchantMoeAdapter.sol";
import {LendleAdapter} from "../src/adapters/LendleAdapter.sol";
import {MethProtocolAdapter} from "../src/adapters/MethProtocolAdapter.sol";
import {EthenaAdapter} from "../src/adapters/EthenaAdapter.sol";
import {AaveV3WethAdapter} from "../src/adapters/AaveV3WethAdapter.sol";
import {AaveV3UsdcAdapter} from "../src/adapters/AaveV3UsdcAdapter.sol";
import {MerchantMoeRouter} from "../src/adapters/MerchantMoeRouter.sol";
import {BybitAttestor} from "../src/adapters/BybitAttestor.sol";

// Mantle Mainnet addresses (forked into anvil — same addresses).
address constant WETH  = 0xdEAddEaDdeadDEadDEADDEAddEADDEAddead1111;
address constant USDC  = 0x09Bc4E0D864854c6aFB6eB9A9cdF58aC190D0dF9;
address constant USDE  = 0x5d3a1Ff2b6BAb83b63cd9AD0787074081a52ef34;

address constant AAVE_POOL   = 0x458F293454fE0d67EC0655f3672301301DD51422;
address constant AAVE_ORACLE = 0x47a063CfDa980532267970d478EC340C0F80E8df;
address constant AWETH       = 0xeAC30Ed8609F564aE65C809C4bf42dB2fF426D2C;
address constant AUSDC       = 0xcb8164415274515867ec43CbD284ab5d6d2b304F;

address constant LB_ROUTER = 0x013e138EF6008ae5FDFDE29700e3f2Bc61d21E3a;

// Canonical ERC-8004 ReputationRegistry on Mantle mainnet (forked into
// anvil — present at the same address).
address constant REPUTATION_REGISTRY_8004 = 0x8004BAa17C55a88189AE136b182e5fdA19dE9b63;

/// @notice One-shot atomic deploy for LOCAL DEVNET (anvil fork) only.
///
/// Deploys the full stack in a single broadcast — CapitalManager, vUSDC,
/// DecisionLog, ReputationOracle, all adapters — wires them together
/// (`setVusdc`, `whitelistStrategy`, `setAgent`) and leaves the deployer
/// as owner (no Safe in local dev).
///
/// For mainnet use Deploy.s.sol (two-phase, transfers ownership to Safe).
contract DeployLocal is Script {
    function run() external {
        address deployer = vm.envAddress("DEPLOYER_ADDRESS");
        address agentAddress = vm.envOr("AGENT_ADDRESS", deployer);
        address attestorAddress = vm.envOr("ATTESTOR_ADDRESS", deployer);
        // Defaults to canonical ERC-8004 on Mantle (forked into anvil).
        // Override via REGISTRY_8004 env if needed (e.g. deploying a stub
        // registry first for non-fork local devnets).
        address registry8004 = vm.envOr("REGISTRY_8004", REPUTATION_REGISTRY_8004);
        uint256 agentId = vm.envOr("AGENT_ID", uint256(1));
        address sequencerFeed = vm.envOr("SEQUENCER_FEED", address(0));

        console.log("DEPLOYER_ADDRESS:", deployer);
        console.log("AGENT_ADDRESS:   ", agentAddress);
        console.log("ATTESTOR_ADDRESS:", attestorAddress);

        vm.startBroadcast();

        // 1. CapitalManager (raw USDC pool).
        CapitalManager vault = new CapitalManager(IERC20(USDC), deployer, sequencerFeed);
        console.log("CapitalManager:    ", address(vault));

        // 2. vUSDC token (immutable ref to CapitalManager).
        VUSDC vusdc = new VUSDC(ICapitalManager(address(vault)));
        console.log("VUSDC:             ", address(vusdc));

        // 3. DecisionLog.
        DecisionLog decisionLog = new DecisionLog(deployer);
        console.log("DecisionLog:       ", address(decisionLog));

        // 4. ReputationOracle (reads vUSDC.exchangeRate via ICapitalManager alias).
        //    registry8004 == address(0) in local — oracle still deploys but
        //    submit() will be a no-op until a real registry is set.
        ReputationOracle oracle = new ReputationOracle(address(vusdc), registry8004, agentId);
        console.log("ReputationOracle:  ", address(oracle));

        // 5. Adapters (stubs + real).
        MerchantMoeAdapter mmAdapter = new MerchantMoeAdapter(address(vault));
        LendleAdapter lendleAdapter = new LendleAdapter(address(vault));
        MethProtocolAdapter methAdapter = new MethProtocolAdapter(address(vault));
        EthenaAdapter ethenaAdapter = new EthenaAdapter(address(vault));
        MerchantMoeRouter moeRouter = new MerchantMoeRouter(LB_ROUTER, USDC, USDE, address(0), deployer);

        AaveV3WethAdapter wethAdapter = new AaveV3WethAdapter(
            AAVE_POOL, AAVE_ORACLE, WETH, AWETH, USDC, address(vault), deployer
        );
        console.log("AaveV3WethAdapter: ", address(wethAdapter));

        AaveV3UsdcAdapter usdcAdapter = new AaveV3UsdcAdapter(
            AAVE_POOL, USDC, AUSDC, address(vault), deployer
        );
        console.log("AaveV3UsdcAdapter: ", address(usdcAdapter));

        BybitAttestor bybitAttestor = new BybitAttestor(
            USDC, address(vault), attestorAddress, deployer
        );
        console.log("BybitAttestor:     ", address(bybitAttestor));

        // 6. Wire: whitelist real adapters, set agent, bind vUSDC.
        vault.whitelistStrategy(address(wethAdapter), true);
        vault.whitelistStrategy(address(usdcAdapter), true);
        vault.whitelistStrategy(address(bybitAttestor), true);

        vault.setAgent(agentAddress);
        decisionLog.setAgent(agentAddress);
        vault.setVusdc(address(vusdc));

        vm.stopBroadcast();

        // Silence unused-warning for stub adapters & router (kept for
        // address reservation parity with Deploy.s.sol).
        mmAdapter; lendleAdapter; methAdapter; ethenaAdapter; moeRouter;

        console.log("");
        console.log("=== LOCAL DEPLOYMENT (deployer remains owner) ===");
        console.log("CapitalManager:    %s", address(vault));
        console.log("VUSDC:             %s", address(vusdc));
        console.log("DecisionLog:       %s", address(decisionLog));
        console.log("ReputationOracle:  %s", address(oracle));
        console.log("AaveV3UsdcAdapter: %s", address(usdcAdapter));
        console.log("AaveV3WethAdapter: %s", address(wethAdapter));
        console.log("BybitAttestor:     %s", address(bybitAttestor));
    }
}
