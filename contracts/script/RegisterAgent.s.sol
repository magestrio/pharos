// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Script, console} from "forge-std/Script.sol";

// ERC-8004 IdentityRegistry on Mantle Mainnet (canonical, ERC1967 proxy).
// Source: notes/erc-8004.md (verified on Mantlescan + github.com/erc-8004/erc-8004-contracts).
address constant IDENTITY_REGISTRY = 0x8004A169FB4a3325136EB29fA0ceB6D2e539a432;

interface IIdentityRegistry {
    /// Permissionless. msg.sender becomes owner of the minted agent NFT.
    /// Emits Registered(uint256 indexed agentId, string agentURI, address indexed owner).
    function register(string calldata agentURI) external returns (uint256 agentId);
}

/// @notice Generates the calldata for IdentityRegistry.register(agentURI) so the
///         Gnosis Safe (2/3) can paste it into Safe Transaction Builder.
///
///         We do NOT broadcast. The Safe must be the actual caller — it ends up
///         as `ownerOf(agentId)`, matching the design in notes/erc-8004.md.
///         Direct broadcast would mint to deployer EOA and force a second
///         `safeTransferFrom` tx, which we explicitly rejected.
///
/// Inputs:
///   AGENT_URI — ipfs://<cid> of the metadata JSON (subtask .2 output).
///
/// Output: prints `to`, `value`, `data` for Safe TxBuilder "Custom data" tx.
///         After the Safe executes, run `scripts/extract-agent-id.sh <txHash>`
///         to read AGENT_ID from the Registered event.
contract RegisterAgent is Script {
    function run() external view {
        string memory agentURI = vm.envString("AGENT_URI");
        require(bytes(agentURI).length > 0, "AGENT_URI empty");

        bytes memory data = abi.encodeWithSelector(IIdentityRegistry.register.selector, agentURI);

        console.log("==== Safe Transaction Builder inputs ====");
        console.log("to:        ", IDENTITY_REGISTRY);
        console.log("value:      0");
        console.log("signature:  register(string)");
        console.log("selector:  ", vm.toString(abi.encodePacked(IIdentityRegistry.register.selector)));
        console.log("agentURI:  ", agentURI);
        console.log("data:");
        console.logBytes(data);
    }
}
