// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IStrategyAdapter} from "./IStrategyAdapter.sol";
import {IERC4626} from "@openzeppelin/contracts/interfaces/IERC4626.sol";

contract EthenaAdapter is IStrategyAdapter {
    address public immutable vault;
    uint256 public cooldownStarted;

    constructor(address _vault) {
        vault = _vault;
    }

    function deposit(uint256 /*amount*/) external pure override {
        revert("EthenaAdapter: not implemented");
    }

    function withdraw(uint256 /*amount*/) external pure override returns (uint256) {
        revert("EthenaAdapter: not implemented");
    }

    function balance() external pure override returns (uint256) {
        return 0;
    }

    function asset() external view override returns (address) {
        return IERC4626(vault).asset();
    }

    function cooldownStart() external {
        cooldownStarted = block.timestamp;
    }

    function instantExit() external {
        // stub: emergency exit, accepts penalty
        cooldownStarted = 0;
    }
}
