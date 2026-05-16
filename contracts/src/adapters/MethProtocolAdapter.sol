// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IStrategyAdapter} from "./IStrategyAdapter.sol";

contract MethProtocolAdapter is IStrategyAdapter {
    address public immutable vault;

    constructor(address _vault) {
        vault = _vault;
    }

    function execute(bytes calldata /*data*/) external override {
        // stub: stake/unstake mETH via mETH protocol
    }

    function balance() external view override returns (uint256) {
        return 0;
    }
}
