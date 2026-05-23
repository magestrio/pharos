// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {ERC4626} from "@openzeppelin/contracts/token/ERC20/extensions/ERC4626.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {EnumerableSet} from "@openzeppelin/contracts/utils/structs/EnumerableSet.sol";
import {IStrategyAdapter} from "./adapters/IStrategyAdapter.sol";

contract Vault8004 is ERC4626, Ownable, Pausable, ReentrancyGuard {
    using SafeERC20 for IERC20;
    using EnumerableSet for EnumerableSet.AddressSet;

    enum AllocationCallKind { Deposit, Withdraw }

    struct AllocationCall {
        address adapter;
        AllocationCallKind kind;
        uint256 amount;
    }

    address public agent;
    EnumerableSet.AddressSet private _whitelisted;

    event AgentSet(address indexed agent);
    event StrategyWhitelisted(address indexed strategy, bool status);
    event CallExecuted(
        bytes32 indexed decisionId,
        uint256 indexed callIndex,
        address indexed adapter,
        AllocationCallKind kind,
        uint256 requested,
        uint256 actualResult
    );
    event AllocationExecuted(bytes32 indexed decisionId, uint256 totalAssetsAfter);
    event EmergencyWithdrawn(address indexed strategy, uint256 received);

    modifier onlyAgent() {
        require(msg.sender == agent, "not agent");
        _;
    }

    constructor(
        IERC20 _asset,
        address _owner,
        string memory _name,
        string memory _symbol
    ) ERC4626(_asset) ERC20(_name, _symbol) Ownable(_owner) {}

    // ─── ERC-4626 overrides ──────────────────────────────────────────────────

    function totalAssets() public view override returns (uint256) {
        uint256 sum = IERC20(asset()).balanceOf(address(this));
        uint256 n = _whitelisted.length();
        for (uint256 i = 0; i < n; ++i) {
            sum += IStrategyAdapter(_whitelisted.at(i)).valueInBaseAsset();
        }
        return sum;
    }

    // Pause + reentrancy guard applied at the internal hook level so all four
    // public entry points (deposit/mint/withdraw/redeem) inherit the protection.
    function _deposit(address caller, address receiver, uint256 assets, uint256 shares)
        internal
        override
        nonReentrant
        whenNotPaused
    {
        super._deposit(caller, receiver, assets, shares);
    }

    function _withdraw(address caller, address receiver, address owner, uint256 assets, uint256 shares)
        internal
        override
        nonReentrant
        whenNotPaused
    {
        super._withdraw(caller, receiver, owner, assets, shares);
    }

    // ─── Whitelist views ─────────────────────────────────────────────────────

    function isWhitelisted(address strategy) external view returns (bool) {
        return _whitelisted.contains(strategy);
    }

    function whitelistedCount() external view returns (uint256) {
        return _whitelisted.length();
    }

    function whitelistedAt(uint256 i) external view returns (address) {
        return _whitelisted.at(i);
    }

    // ─── Owner-only ──────────────────────────────────────────────────────────

    function setAgent(address _agent) external onlyOwner {
        require(_agent != address(0), "zero agent");
        agent = _agent;
        emit AgentSet(_agent);
    }

    function whitelistStrategy(address strategy, bool status) external onlyOwner {
        require(strategy != address(0), "zero strategy");
        if (status) {
            _whitelisted.add(strategy);
        } else {
            _whitelisted.remove(strategy);
        }
        emit StrategyWhitelisted(strategy, status);
    }

    function pause() external onlyOwner {
        _pause();
    }

    function unpause() external onlyOwner {
        _unpause();
    }

    function emergencyWithdraw(address strategy) external nonReentrant onlyOwner {
        uint256 bal = IStrategyAdapter(strategy).balance();
        uint256 received = 0;
        if (bal > 0) {
            received = IStrategyAdapter(strategy).withdraw(bal);
        }
        emit EmergencyWithdrawn(strategy, received);
    }

    // ─── Agent-only ──────────────────────────────────────────────────────────

    /// @notice Atomically execute a sequence of adapter calls.
    /// @param decisionId IPFS-CID-hashed pointer to the rationale that authored
    ///                   this allocation. Logged on DecisionLog off-band.
    /// @param calls Ordered list of deposit/withdraw calls against whitelisted adapters.
    /// @param minTotalAssetsAfter Slippage / oracle-manipulation guard: revert if
    ///                            post-execution `totalAssets()` falls below this.
    function executeAllocation(
        bytes32 decisionId,
        AllocationCall[] calldata calls,
        uint256 minTotalAssetsAfter
    ) external nonReentrant whenNotPaused onlyAgent {
        require(calls.length > 0, "empty calls");

        for (uint256 i = 0; i < calls.length; ++i) {
            AllocationCall calldata c = calls[i];
            require(_whitelisted.contains(c.adapter), "not whitelisted");

            uint256 actual;
            if (c.kind == AllocationCallKind.Deposit) {
                address adapterAsset = IStrategyAdapter(c.adapter).asset();
                IERC20(adapterAsset).forceApprove(c.adapter, c.amount);
                IStrategyAdapter(c.adapter).deposit(c.amount);
                actual = c.amount;
            } else {
                actual = IStrategyAdapter(c.adapter).withdraw(c.amount);
            }
            emit CallExecuted(decisionId, i, c.adapter, c.kind, c.amount, actual);
        }

        uint256 ta = totalAssets();
        require(ta >= minTotalAssetsAfter, "slippage");
        emit AllocationExecuted(decisionId, ta);
    }
}
