// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {CapitalManager} from "../src/CapitalManager.sol";
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

// Mantle Mainnet addresses (verified via bgd-labs/aave-address-book)
address constant WETH  = 0xdEAddEaDdeadDEadDEADDEAddEADDEAddead1111;
address constant USDC  = 0x09Bc4E0D864854c6aFB6eB9A9cdF58aC190D0dF9;
address constant METH  = 0xcDA86A272531e8640cD7F1a92c01839911B90bb0;
address constant CMETH = 0xE6829d9a7eE3040e1276Fa75293Bde931859e8fA;
address constant USDE  = 0x5d3a1Ff2b6BAb83b63cd9AD0787074081a52ef34;

// Aave V3 Mantle
address constant AAVE_POOL                    = 0x458F293454fE0d67EC0655f3672301301DD51422;
address constant AAVE_POOL_ADDRESSES_PROVIDER = 0xba50Cd2A20f6DA35D788639E581bca8d0B5d4D5f;
address constant AAVE_DATA_PROVIDER           = 0x487c5c669D9eee6057C44973207101276cf73b68;
address constant AAVE_ORACLE                  = 0x47a063CfDa980532267970d478EC340C0F80E8df;
address constant AWETH                        = 0xeAC30Ed8609F564aE65C809C4bf42dB2fF426D2C;
address constant AUSDC                        = 0xcb8164415274515867ec43CbD284ab5d6d2b304F;

// Merchant Moe
address constant LB_ROUTER = 0x013e138EF6008ae5FDFDE29700e3f2Bc61d21E3a;

/// @notice Two-phase deployment for the post-pivot vUSDC architecture.
///
/// Phase A — VUSDC_ADDR env unset:
///   Deploys CapitalManager + adapters + setAgent. Deployer remains owner so
///   the wiring of vUSDC (which is deployed in a separate epic) can be done
///   atomically with `setVusdc` + ownership transfer in Phase B.
///
/// Phase B — VUSDC_ADDR env set:
///   Re-running with VUSDC_ADDR after vUSDC is live: the script (assuming the
///   manager is still owned by the broadcaster) will call `setVusdc(VUSDC_ADDR)`
///   one-shot, then transfer ownership to SAFE_OWNER.
///
/// The wiring of an already-deployed CapitalManager (just setVusdc + transfer)
/// belongs in a follow-up script under the `vusdc-token` / `mainnet-deploy`
/// epics. This script is the cold-start path.
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
        // ATTESTOR_ADDRESS = Safe (2-of-3) controlling the Bybit account that
        // confirms BybitAttestor deposits/withdraws and pushes balance updates.
        // Falls back to deployer for dev. Mainnet MUST set this to the Safe.
        address attestorAddress = vm.envOr("ATTESTOR_ADDRESS", deployer);
        // vUSDC token address. If unset, the deploy stops short of setVusdc +
        // transferOwnership so a follow-up phase can wire vUSDC atomically.
        address vusdcAddr = vm.envOr("VUSDC_ADDR", address(0));

        console.log("DEPLOYER_ADDRESS:   ", deployer);
        console.log("SAFE_OWNER:         ", safeOwner);
        console.log("AGENT_ADDRESS:      ", agentAddress);
        console.log("ATTESTOR_ADDRESS:   ", attestorAddress);
        console.log("VUSDC_ADDR:         ", vusdcAddr);
        if (agentAddress == deployer) {
            console.log("WARN: agentAddress == deployer. Set AGENT_ADDRESS env var before mainnet broadcast.");
        }
        if (attestorAddress == deployer) {
            console.log("WARN: attestorAddress == deployer. Set ATTESTOR_ADDRESS env var (the Safe) before mainnet broadcast.");
        }
        if (vusdcAddr == address(0)) {
            console.log("INFO: VUSDC_ADDR unset - Phase A (deployer remains owner; setVusdc + transfer deferred).");
        }

        vm.startBroadcast();

        // Chainlink L2 Sequencer Uptime Feed on Mantle — set via SEQUENCER_FEED env.
        // Pass address(0) to disable the check (default until feed address is verified).
        // See notes/addresses.md — research pending.
        address sequencerFeed = vm.envOr("SEQUENCER_FEED", address(0));

        // 1. Deploy CapitalManager (raw USDC capital pool, post-vUSDC pivot).
        CapitalManager vault = new CapitalManager(IERC20(USDC), deployer, sequencerFeed);
        console.log("CapitalManager:     ", address(vault));
        console.log("USDC:               ", address(vault.usdc()));

        DecisionLog decisionLog = new DecisionLog(deployer);
        console.log("DecisionLog:        ", address(decisionLog));

        ReputationOracle oracle = new ReputationOracle(
            address(vault), registry8004, agentId
        );
        console.log("ReputationOracle:   ", address(oracle));

        // Stub adapters: deployed for address reservation, revert on
        // deposit/withdraw. Not whitelisted (would burn gas in totalAssetsUsdc).
        MerchantMoeAdapter mmAdapter = new MerchantMoeAdapter(address(vault));
        console.log("MerchantMoeAdapter: ", address(mmAdapter));

        LendleAdapter lendleAdapter = new LendleAdapter(address(vault));
        console.log("LendleAdapter:      ", address(lendleAdapter));

        MethProtocolAdapter methAdapter = new MethProtocolAdapter(address(vault));
        console.log("MethProtocolAdapter:", address(methAdapter));

        EthenaAdapter ethenaAdapter = new EthenaAdapter(address(vault));
        console.log("EthenaAdapter:      ", address(ethenaAdapter));

        // Real adapters. MerchantMoeRouter kept for future WETH→USDC swap leg,
        // uses placeholder address(0) for the sUSDe slot (out-of-MVP).
        MerchantMoeRouter moeRouter = new MerchantMoeRouter(LB_ROUTER, USDC, USDE, address(0), deployer);
        console.log("MoeRouter:          ", address(moeRouter));

        AaveV3WethAdapter wethAdapter = new AaveV3WethAdapter(
            AAVE_POOL, AAVE_ORACLE, WETH, AWETH, USDC, address(vault), deployer
        );
        console.log("WethAdapter:        ", address(wethAdapter));

        AaveV3UsdcAdapter usdcAdapter = new AaveV3UsdcAdapter(
            AAVE_POOL, USDC, AUSDC, address(vault), deployer
        );
        console.log("UsdcAdapter:        ", address(usdcAdapter));

        BybitAttestor bybitAttestor = new BybitAttestor(
            USDC, address(vault), attestorAddress, deployer
        );
        console.log("BybitAttestor:      ", address(bybitAttestor));

        // 2. Whitelist real adapters + set agent. Done BEFORE setVusdc/transfer
        // so these calls go through deployer EOA, not 2/3 Safe signatures.
        vault.whitelistStrategy(address(wethAdapter), true);
        vault.whitelistStrategy(address(usdcAdapter), true);
        vault.whitelistStrategy(address(bybitAttestor), true);

        vault.setAgent(agentAddress);
        decisionLog.setAgent(agentAddress);

        // 3. Wire vUSDC (Phase B only). setVusdc is one-shot; once set, the
        // recordDeposit/recordWithdraw entry points become callable from vUSDC.
        if (vusdcAddr != address(0)) {
            vault.setVusdc(vusdcAddr);
            console.log("setVusdc executed:  ", vusdcAddr);
        }

        // 4. Transfer ownership to Gnosis Safe (2-of-3). Deferred in Phase A so
        // the deployer can call setVusdc atomically in Phase B without needing
        // Safe signatures for the one-shot setter.
        if (vusdcAddr != address(0)) {
            vault.transferOwnership(safeOwner);
            decisionLog.transferOwnership(safeOwner);
            console.log("Ownership transferred to Safe:", safeOwner);
        } else {
            console.log("Ownership NOT transferred (Phase A). Re-run with VUSDC_ADDR set.");
        }

        vm.stopBroadcast();

        console.log("");
        console.log("=== DEPLOYMENT SUMMARY (copy to notes/addresses.md) ===");
        console.log("CapitalManager:      %s", address(vault));
        console.log("DecisionLog:         %s", address(decisionLog));
        console.log("ReputationOracle:    %s", address(oracle));
        console.log("AaveV3WethAdapter:   %s", address(wethAdapter));
        console.log("AaveV3UsdcAdapter:   %s", address(usdcAdapter));
        console.log("BybitAttestor:       %s", address(bybitAttestor));
        console.log("MerchantMoeRouter:   %s", address(moeRouter));
        console.log("MerchantMoeAdapter:  %s (stub, not whitelisted)", address(mmAdapter));
        console.log("LendleAdapter:       %s (stub, not whitelisted)", address(lendleAdapter));
        console.log("MethProtocolAdapter: %s (stub, not whitelisted)", address(methAdapter));
        console.log("EthenaAdapter:       %s (stub, not whitelisted)", address(ethenaAdapter));
    }
}
