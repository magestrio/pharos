// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

interface IStrategyAdapter {
    function execute(bytes calldata data) external;
    function balance() external view returns (uint256);
}
