// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IStrategyAdapter} from "./IStrategyAdapter.sol";
import {IERC4626} from "@openzeppelin/contracts/interfaces/IERC4626.sol";

contract LendleAdapter is IStrategyAdapter {
    address public immutable vault;

    constructor(address _vault) {
        vault = _vault;
    }

    function deposit(uint256 /*amount*/) external override {
        // stub: supply mETH on Lendle
    }

    function withdraw(uint256 /*amount*/) external override {
        // stub: withdraw mETH from Lendle
    }

    function balance() external pure override returns (uint256) {
        return 0;
    }

    function asset() external view override returns (address) {
        return IERC4626(vault).asset();
    }
}
