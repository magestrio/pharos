// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

contract DecisionLog is Ownable {
    event DecisionRecorded(
        uint256 indexed agentId,
        bytes32 indexed decisionId,
        string ipfsCid,
        bytes32 actionHash,
        uint256 timestamp
    );

    constructor(address _owner) Ownable(_owner) {}

    function recordDecision(
        uint256 agentId,
        bytes32 decisionId,
        string calldata ipfsCid,
        bytes32 actionHash
    ) external onlyOwner {
        emit DecisionRecorded(agentId, decisionId, ipfsCid, actionHash, block.timestamp);
    }
}
