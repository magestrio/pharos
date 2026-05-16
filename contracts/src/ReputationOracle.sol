// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

interface IVault {
    function totalAssets() external view returns (uint256);
}

interface IReputation8004Registry {
    function submit(uint256 agentId, uint256 score) external;
}

contract ReputationOracle {
    uint256 public constant MIN_INTERVAL = 1 hours;

    address public immutable vault;
    address public immutable registry8004;
    uint256 public immutable agentId;
    uint256 public immutable initialDeposit;

    uint256 public lastUpdate;
    uint256 public lastScore;

    event ReputationUpdated(uint256 score, uint256 timestamp);

    constructor(address _vault, address _registry8004, uint256 _agentId, uint256 _initialDeposit) {
        vault = _vault;
        registry8004 = _registry8004;
        agentId = _agentId;
        initialDeposit = _initialDeposit;
    }

    function updateReputation() external {
        require(block.timestamp >= lastUpdate + MIN_INTERVAL, "too soon");

        uint256 current = IVault(vault).totalAssets();

        // APR in bps: (current - initialDeposit) / initialDeposit * 10000
        uint256 score = 0;
        if (initialDeposit > 0 && current > initialDeposit) {
            score = ((current - initialDeposit) * 10_000) / initialDeposit;
        }

        lastScore = score;
        lastUpdate = block.timestamp;

        // Submit to canonical ERC-8004 registry (no-op if registry not set)
        if (registry8004 != address(0)) {
            IReputation8004Registry(registry8004).submit(agentId, score);
        }

        emit ReputationUpdated(score, block.timestamp);
    }
}
