// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {IStrategyAdapter} from "./adapters/IStrategyAdapter.sol";

contract Vault8004 is Ownable, Pausable {
    using SafeERC20 for IERC20;

    struct AllocationCall {
        address adapter;
        bytes data;
    }

    IERC20 public immutable asset;
    mapping(address => bool) public whitelistedAdapters;

    event Deposited(address indexed user, uint256 amount);
    event Withdrawn(address indexed user, uint256 amount);
    event AllocationExecuted(bytes32 indexed decisionId, uint256 callCount);
    event AdapterWhitelisted(address indexed adapter, bool status);

    constructor(address _asset, address _owner) Ownable(_owner) {
        asset = IERC20(_asset);
    }

    function deposit(uint256 amount) external whenNotPaused {
        asset.safeTransferFrom(msg.sender, address(this), amount);
        emit Deposited(msg.sender, amount);
    }

    function withdraw(uint256 amount) external onlyOwner {
        asset.safeTransfer(msg.sender, amount);
        emit Withdrawn(msg.sender, amount);
    }

    function executeAllocation(
        bytes32 decisionId,
        AllocationCall[] calldata calls
    ) external onlyOwner whenNotPaused {
        for (uint256 i = 0; i < calls.length; i++) {
            require(whitelistedAdapters[calls[i].adapter], "adapter not whitelisted");
            IStrategyAdapter(calls[i].adapter).execute(calls[i].data);
        }
        emit AllocationExecuted(decisionId, calls.length);
    }

    function setAdapterWhitelist(address adapter, bool status) external onlyOwner {
        whitelistedAdapters[adapter] = status;
        emit AdapterWhitelisted(adapter, status);
    }

    function pause() external onlyOwner {
        _pause();
    }

    function unpause() external onlyOwner {
        _unpause();
    }

    function totalAssets() external view returns (uint256) {
        return asset.balanceOf(address(this));
    }
}
