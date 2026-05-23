// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {ERC4626} from "@openzeppelin/contracts/token/ERC20/extensions/ERC4626.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {IStrategyAdapter} from "./adapters/IStrategyAdapter.sol";

contract Vault8004 is ERC4626, Ownable, Pausable, ReentrancyGuard {
    using SafeERC20 for IERC20;

    // NOTE: ERC4626 already exposes asset() → address. We do NOT redeclare
    // `IERC20 public immutable asset` here — that would generate a getter with a
    // mismatched return type (IERC20 vs address) and fail to compile.
    // Use IERC20(asset()) wherever the token reference is needed internally.

    address public agent;
    address public currentStrategy;
    mapping(address => bool) public whitelistedStrategies;
    uint256 public totalAllocated;

    event AgentSet(address indexed agent);
    event StrategyWhitelisted(address indexed strategy, bool status);
    event StrategyChanged(address indexed oldStrategy, address indexed newStrategy);
    event Allocated(address indexed strategy, uint256 amount, bytes32 indexed decisionId);
    event Deallocated(address indexed strategy, uint256 amount, bytes32 indexed decisionId);

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
        uint256 strategyBal = currentStrategy != address(0)
            ? IStrategyAdapter(currentStrategy).balance()
            : 0;
        return IERC20(asset()).balanceOf(address(this)) + strategyBal;
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

    // ─── Owner-only ──────────────────────────────────────────────────────────

    function setAgent(address _agent) external onlyOwner {
        require(_agent != address(0), "zero agent");
        agent = _agent;
        emit AgentSet(_agent);
    }

    function whitelistStrategy(address strategy, bool status) external onlyOwner {
        whitelistedStrategies[strategy] = status;
        emit StrategyWhitelisted(strategy, status);
    }

    function setCurrentStrategy(address strategy) external onlyOwner {
        require(whitelistedStrategies[strategy], "not whitelisted");
        address old = currentStrategy;
        currentStrategy = strategy;
        emit StrategyChanged(old, strategy);
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
        if (strategy == currentStrategy) {
            totalAllocated = 0;
        }
        emit Deallocated(strategy, received, bytes32(0));
    }

    // ─── Agent-only ──────────────────────────────────────────────────────────

    function allocate(bytes32 decisionId, uint256 amount) external nonReentrant whenNotPaused onlyAgent {
        require(currentStrategy != address(0), "no strategy");
        uint256 free = IERC20(asset()).balanceOf(address(this)) - totalAllocated;
        require(amount <= free, "insufficient free cash");
        // CEI: update bookkeeping before the external call. If deposit() reverts,
        // the entire tx reverts and totalAllocated unwinds with it.
        totalAllocated += amount;
        IERC20(asset()).forceApprove(currentStrategy, amount);
        IStrategyAdapter(currentStrategy).deposit(amount);
        emit Allocated(currentStrategy, amount, decisionId);
    }

    function deallocate(bytes32 decisionId, uint256 amount) external nonReentrant whenNotPaused onlyAgent {
        require(currentStrategy != address(0), "no strategy");
        // Clamp requested amount to the adapter's actual balance. Some protocols
        // (Aave V3, sUSDe stake-via-router) round aToken/share minting down by 1 wei
        // on deposit, so withdrawing the original deposit amount would revert.
        // Also: accrued interest can push balance above totalAllocated — clamping
        // here keeps totalAllocated bookkeeping consistent with what was withdrawn.
        uint256 bal = IStrategyAdapter(currentStrategy).balance();
        if (amount > bal) amount = bal;
        uint256 received = IStrategyAdapter(currentStrategy).withdraw(amount);
        if (IStrategyAdapter(currentStrategy).balance() == 0) {
            // Full exit — zero out bookkeeping regardless of rounding losses or
            // accrued interest, since the strategy holds nothing on our behalf.
            totalAllocated = 0;
        } else {
            totalAllocated = received >= totalAllocated ? 0 : totalAllocated - received;
        }
        emit Deallocated(currentStrategy, received, decisionId);
    }
}
