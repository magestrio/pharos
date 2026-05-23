// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Vault8004} from "../src/Vault8004.sol";
import {DecisionLog} from "../src/DecisionLog.sol";
import {ReputationOracle} from "../src/ReputationOracle.sol";
import {MerchantMoeAdapter} from "../src/adapters/MerchantMoeAdapter.sol";
import {LendleAdapter} from "../src/adapters/LendleAdapter.sol";
import {MethProtocolAdapter} from "../src/adapters/MethProtocolAdapter.sol";
import {EthenaAdapter} from "../src/adapters/EthenaAdapter.sol";
import {AaveV3WethAdapter} from "../src/adapters/AaveV3WethAdapter.sol";
import {AaveV3UsdcAdapter} from "../src/adapters/AaveV3UsdcAdapter.sol";
import {MerchantMoeRouter} from "../src/adapters/MerchantMoeRouter.sol";

// Mantle Mainnet addresses (verified via bgd-labs/aave-address-book)
address constant WETH  = 0xdEAddEaDdeadDEadDEADDEAddEADDEAddead1111;
address constant USDC  = 0x09Bc4E0D864854c6aFB6eB9A9cdF58aC190D0dF9;
address constant METH  = 0xcDA86A272531e8640cD7F1a92c01839911B90bb0;
address constant CMETH = 0xE6829d9a7eE3040e1276Fa75293Bde931859e8fA;
address constant SUSDE = 0x211Cc4DD073734dA055fbF44a2b4667d5E5fE5d2;
address constant USDE  = 0x5d3a1Ff2b6BAb83b63cd9AD0787074081a52ef34;

// Aave V3 Mantle
address constant AAVE_POOL                    = 0x458F293454fE0d67EC0655f3672301301DD51422;
address constant AAVE_POOL_ADDRESSES_PROVIDER = 0xba50Cd2A20f6DA35D788639E581bca8d0B5d4D5f;
address constant AAVE_DATA_PROVIDER           = 0x487c5c669D9eee6057C44973207101276cf73b68;
address constant AWETH                        = 0xeAC30Ed8609F564aE65C809C4bf42dB2fF426D2C;
address constant AUSDC                        = 0xcb8164415274515867ec43CbD284ab5d6d2b304F;
address constant ASUSDE                       = 0xaf972F332FF79bd32A6CB6B54f903eA0F9b16C2a;

// Merchant Moe
address constant LB_ROUTER = 0x013e138EF6008ae5FDFDE29700e3f2Bc61d21E3a;

contract Deploy is Script {
    function run() external {
        address deployer = vm.envAddress("DEPLOYER_ADDRESS");
        address safeOwner = vm.envAddress("SAFE_OWNER");
        address registry8004 = vm.envOr("REGISTRY_8004", address(0));
        uint256 agentId = vm.envOr("AGENT_ID", uint256(1));
        // AGENT_ADDRESS = operational EOA that calls allocate/deallocate.
        // Falls back to deployer if not set — useful for sepolia smoke runs.
        // For mainnet this MUST be explicitly set to the AI agent's address.
        address agentAddress = vm.envOr("AGENT_ADDRESS", deployer);

        console.log("DEPLOYER_ADDRESS:   ", deployer);
        console.log("SAFE_OWNER:         ", safeOwner);
        console.log("AGENT_ADDRESS:      ", agentAddress);
        if (agentAddress == deployer) {
            console.log("WARN: agentAddress == deployer. Set AGENT_ADDRESS env var before mainnet broadcast.");
        }

        vm.startBroadcast();

        // Chainlink L2 Sequencer Uptime Feed on Mantle — set via SEQUENCER_FEED env.
        // Pass address(0) to disable the check (default until feed address is verified).
        // See notes/addresses.md — research pending.
        address sequencerFeed = vm.envOr("SEQUENCER_FEED", address(0));

        Vault8004 vault = new Vault8004(
            IERC20(WETH), deployer, "Vault8004 WETH", "v8004-WETH", sequencerFeed
        );
        console.log("Vault8004:          ", address(vault));
        console.log("asset (WETH):       ", address(vault.asset()));

        DecisionLog decisionLog = new DecisionLog(deployer);
        console.log("DecisionLog:        ", address(decisionLog));

        ReputationOracle oracle = new ReputationOracle(
            address(vault), registry8004, agentId
        );
        console.log("ReputationOracle:   ", address(oracle));

        MerchantMoeAdapter mmAdapter = new MerchantMoeAdapter(address(vault));
        console.log("MerchantMoeAdapter: ", address(mmAdapter));

        LendleAdapter lendleAdapter = new LendleAdapter(address(vault));
        console.log("LendleAdapter:      ", address(lendleAdapter));

        MethProtocolAdapter methAdapter = new MethProtocolAdapter(address(vault));
        console.log("MethProtocolAdapter:", address(methAdapter));

        EthenaAdapter ethenaAdapter = new EthenaAdapter(address(vault));
        console.log("EthenaAdapter:      ", address(ethenaAdapter));

        // Stub adapters are deployed for address reservation only; they revert
        // on deposit/withdraw (see .3 security pass). Whitelisting them would
        // burn ~80k gas with zero benefit since allocate() would always revert.

        // Deploy real adapters
        MerchantMoeRouter moeRouter = new MerchantMoeRouter(LB_ROUTER, USDC, USDE, SUSDE, deployer);
        console.log("MoeRouter:          ", address(moeRouter));

        AaveV3WethAdapter wethAdapter = new AaveV3WethAdapter(
            AAVE_POOL, WETH, AWETH, address(vault), deployer
        );
        console.log("WethAdapter:        ", address(wethAdapter));

        AaveV3UsdcAdapter usdcAdapter = new AaveV3UsdcAdapter(
            AAVE_POOL, USDC, AUSDC, address(vault), deployer
        );
        console.log("UsdcAdapter:        ", address(usdcAdapter));

        // Whitelist real adapters
        vault.whitelistStrategy(address(wethAdapter), true);
        vault.whitelistStrategy(address(usdcAdapter), true);

        vault.setAgent(agentAddress);
        decisionLog.setAgent(agentAddress);

        // Transfer ownership to Gnosis Safe (2-of-3) — must be LAST
        // after whitelistStrategy / setAgent, otherwise those setup calls
        // would require 2/3 Safe signatures.
        vault.transferOwnership(safeOwner);
        decisionLog.transferOwnership(safeOwner);
        console.log("Ownership transferred to Safe:", safeOwner);

        vm.stopBroadcast();

        console.log("");
        console.log("=== DEPLOYMENT SUMMARY (copy to notes/addresses.md) ===");
        console.log("Vault8004:           %s", address(vault));
        console.log("DecisionLog:         %s", address(decisionLog));
        console.log("ReputationOracle:    %s", address(oracle));
        console.log("AaveV3WethAdapter:   %s", address(wethAdapter));
        console.log("AaveV3UsdcAdapter:   %s", address(usdcAdapter));
        console.log("MerchantMoeRouter:   %s", address(moeRouter));
        console.log("MerchantMoeAdapter:  %s (stub, not whitelisted)", address(mmAdapter));
        console.log("LendleAdapter:       %s (stub, not whitelisted)", address(lendleAdapter));
        console.log("MethProtocolAdapter: %s (stub, not whitelisted)", address(methAdapter));
        console.log("EthenaAdapter:       %s (stub, not whitelisted)", address(ethenaAdapter));
    }
}
