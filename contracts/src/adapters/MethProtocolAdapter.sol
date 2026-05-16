// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IStrategyAdapter} from "./IStrategyAdapter.sol";
import {IERC4626} from "@openzeppelin/contracts/interfaces/IERC4626.sol";

contract MethProtocolAdapter is IStrategyAdapter {
    address public immutable vault;

    constructor(address _vault) {
        vault = _vault;
    }

    function deposit(uint256 /*amount*/) external override {
        // stub: stake mETH via mETH Protocol
    }

    function withdraw(uint256 /*amount*/) external override {
        // stub: unstake mETH via mETH Protocol
    }

    function balance() external pure override returns (uint256) {
        return 0;
    }

    function asset() external view override returns (address) {
        return IERC4626(vault).asset();
    }
}
