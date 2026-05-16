// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

interface IStrategyAdapter {
    function deposit(uint256 amount) external;
    function withdraw(uint256 amount) external;
    function balance() external view returns (uint256);
    function asset() external view returns (address);
}
