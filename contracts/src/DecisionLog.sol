// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

contract DecisionLog is Ownable {
    uint256 public nextDecisionNonce;
    address public agent;
    mapping(bytes32 => bool) public exists;

    event DecisionRecorded(
        uint256 indexed agentId,
        bytes32 indexed decisionId,
        string ipfsCid,
        bytes32 actionHash,
        uint256 timestamp
    );
    event AgentSet(address indexed agent);

    modifier onlyAgent() {
        require(msg.sender == agent, "not agent");
        _;
    }

    constructor(address _owner) Ownable(_owner) {}

    function setAgent(address _agent) external onlyOwner {
        require(_agent != address(0), "zero agent");
        agent = _agent;
        emit AgentSet(_agent);
    }

    function recordDecision(
        uint256 agentId,
        bytes32 decisionId,
        string calldata ipfsCid,
        bytes32 actionHash
    ) external onlyAgent returns (uint256 nonce) {
        require(!exists[decisionId], "duplicate decision");
        exists[decisionId] = true;
        nonce = nextDecisionNonce++;
        emit DecisionRecorded(agentId, decisionId, ipfsCid, actionHash, block.timestamp);
    }
}
