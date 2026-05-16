// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IStrategyAdapter} from "./IStrategyAdapter.sol";

contract MerchantMoeAdapter is IStrategyAdapter {
    address public immutable vault;

    constructor(address _vault) {
        vault = _vault;
    }

    function execute(bytes calldata /*data*/) external override {
        // stub: interact with Merchant Moe mETH/cmETH pools
    }

    function balance() external view override returns (uint256) {
        return 0;
    }
}
