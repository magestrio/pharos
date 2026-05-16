// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IStrategyAdapter} from "./IStrategyAdapter.sol";

contract LendleAdapter is IStrategyAdapter {
    address public immutable vault;

    constructor(address _vault) {
        vault = _vault;
    }

    function execute(bytes calldata /*data*/) external override {
        // stub: supply/withdraw USDC on Lendle
    }

    function balance() external view override returns (uint256) {
        return 0;
    }
}
