// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {Vault8004} from "../src/Vault8004.sol";
import {DecisionLog} from "../src/DecisionLog.sol";
import {ReputationOracle} from "../src/ReputationOracle.sol";
import {MerchantMoeAdapter} from "../src/adapters/MerchantMoeAdapter.sol";
import {LendleAdapter} from "../src/adapters/LendleAdapter.sol";
import {MethProtocolAdapter} from "../src/adapters/MethProtocolAdapter.sol";
import {EthenaAdapter} from "../src/adapters/EthenaAdapter.sol";

contract Deploy is Script {
    function run() external {
        address deployer = vm.envAddress("DEPLOYER_ADDRESS");
        address asset = vm.envAddress("ASSET_ADDRESS"); // USDC on Mantle
        address registry8004 = vm.envOr("REGISTRY_8004", address(0));
        uint256 agentId = vm.envOr("AGENT_ID", uint256(1));
        uint256 initialDeposit = vm.envOr("INITIAL_DEPOSIT", uint256(1e6));

        vm.startBroadcast();

        Vault8004 vault = new Vault8004(asset, deployer);
        console.log("Vault8004:          ", address(vault));

        DecisionLog decisionLog = new DecisionLog(deployer);
        console.log("DecisionLog:        ", address(decisionLog));

        ReputationOracle oracle = new ReputationOracle(
            address(vault), registry8004, agentId, initialDeposit
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

        // Whitelist adapters
        vault.setAdapterWhitelist(address(mmAdapter), true);
        vault.setAdapterWhitelist(address(lendleAdapter), true);
        vault.setAdapterWhitelist(address(methAdapter), true);
        vault.setAdapterWhitelist(address(ethenaAdapter), true);

        vm.stopBroadcast();
    }
}
