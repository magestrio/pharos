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

        console.log("DEPLOYER_ADDRESS:   ", deployer);
        console.log("SAFE_OWNER:         ", safeOwner);

        vm.startBroadcast();

        Vault8004 vault = new Vault8004(IERC20(WETH), deployer, "Vault8004 WETH", "v8004-WETH");
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

        // Whitelist legacy stub adapters
        vault.whitelistStrategy(address(mmAdapter), true);
        vault.whitelistStrategy(address(lendleAdapter), true);
        vault.whitelistStrategy(address(methAdapter), true);
        vault.whitelistStrategy(address(ethenaAdapter), true);

        // Deploy real adapters
        MerchantMoeRouter moeRouter = new MerchantMoeRouter(LB_ROUTER, WETH, USDC, deployer);
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

        // Default strategy — Aave V3 WETH supply
        vault.setCurrentStrategy(address(wethAdapter));

        // Agent placeholder — update to AI agent address before mainnet
        vault.setAgent(deployer);

        // Transfer ownership to Gnosis Safe (2-of-3) — must be LAST
        // after whitelistStrategy / setCurrentStrategy / setAgent, otherwise
        // those setup calls would require 2/3 Safe signatures.
        vault.transferOwnership(safeOwner);
        decisionLog.transferOwnership(safeOwner);
        console.log("Ownership transferred to Safe:", safeOwner);

        vm.stopBroadcast();
    }
}
