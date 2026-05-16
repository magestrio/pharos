// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IStrategyAdapter} from "./IStrategyAdapter.sol";

contract EthenaAdapter is IStrategyAdapter {
    address public immutable vault;
    uint256 public cooldownStarted;

    constructor(address _vault) {
        vault = _vault;
    }

    function execute(bytes calldata /*data*/) external override {
        // stub: stake/unstake sUSDe via Ethena
    }

    function balance() external view override returns (uint256) {
        return 0;
    }

    function cooldownStart() external {
        cooldownStarted = block.timestamp;
    }

    function instantExit() external {
        // stub: emergency exit, accepts penalty
        cooldownStarted = 0;
    }
}
