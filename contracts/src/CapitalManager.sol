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

/// @notice Chainlink-style L2 Sequencer Uptime Feed minimal surface.
interface IAggregatorV3 {
    function latestRoundData() external view returns (
        uint80 roundId,
        int256 answer,
        uint256 startedAt,
        uint256 updatedAt,
        uint80 answeredInRound
    );
}

contract CapitalManager is ERC4626, Ownable, Pausable, ReentrancyGuard {
    using SafeERC20 for IERC20;
    using EnumerableSet for EnumerableSet.AddressSet;

    enum AllocationCallKind { Deposit, Withdraw }

    struct AllocationCall {
        address adapter;
        AllocationCallKind kind;
        uint256 amount;
    }

    /// @notice Grace period after a sequencer restart during which oracle prices
    ///         may still be stale and `totalAssets()` will revert.
    uint256 public constant SEQUENCER_GRACE_PERIOD = 1 hours;

    /// @notice Chainlink L2 Sequencer Uptime Feed. Zero address disables the check
    ///         (used when no feed is available on the target L2 yet).
    address public immutable sequencerUptimeFeed;

    /// @notice Owner-set cap on total slippage per `executeAllocation`. Basis
    ///         points of `totalAssets()` measured before vs. after the batch.
    ///         Default 100 (1%). Protects against compromised-agent scenarios
    ///         even when the agent passes `minTotalAssetsAfter = 0`.
    uint16 public maxSlippageBps;

    /// @notice Owner-set cap on slippage of any single call within a batch,
    ///         measured against `totalAssets()` immediately before that call.
    ///         Default 100 (1%). Catches a single bad-oracle adapter even when
    ///         the global cap would otherwise be satisfied by gains elsewhere
    ///         in the batch.
    uint16 public maxPerCallLossBps;

    address public agent;
    EnumerableSet.AddressSet private _whitelisted;

    event AgentSet(address indexed agent);
    event StrategyWhitelisted(address indexed strategy, bool status);
    event MaxSlippageBpsSet(uint16 newValue);
    event MaxPerCallLossBpsSet(uint16 newValue);
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
        string memory _symbol,
        address _sequencerUptimeFeed
    ) ERC4626(_asset) ERC20(_name, _symbol) Ownable(_owner) {
        sequencerUptimeFeed = _sequencerUptimeFeed;
        maxSlippageBps      = 100; // 1%
        maxPerCallLossBps   = 100; // 1%
    }

    // ─── ERC-4626 overrides ──────────────────────────────────────────────────

    /// @notice Total assets under management priced in USDC units (6 decimals).
    /// @dev Sums `valueInUsdc()` across every whitelisted adapter plus the
    ///      free USDC balance held directly by the vault.
    ///
    ///      Fail-loud semantics: if ANY adapter's `valueInUsdc()` reverts
    ///      (e.g. its oracle is stale per the rules in IStrategyAdapter), the
    ///      entire call reverts. This blocks all ERC-4626 entry points
    ///      (deposit/mint/withdraw/redeem) until the offending adapter is
    ///      de-whitelisted via `whitelistStrategy(adapter, false)`. This is
    ///      intentional — silently undervaluing a position would dilute existing
    ///      depositors. Operator procedure during a degraded oracle:
    ///        1. `pause()` the vault to halt deposits;
    ///        2. de-whitelist the broken adapter;
    ///        3. resume.
    ///      Owner can also exit funds via `emergencyWithdraw(adapter)` which
    ///      bypasses `totalAssets()`.
    function totalAssets() public view override returns (uint256) {
        _checkSequencer();
        uint256 sum = IERC20(asset()).balanceOf(address(this));
        uint256 n = _whitelisted.length();
        for (uint256 i = 0; i < n; ++i) {
            sum += IStrategyAdapter(_whitelisted.at(i)).valueInUsdc();
        }
        return sum;
    }

    /// @dev Reverts if the configured L2 sequencer is down or has restarted
    ///      within `SEQUENCER_GRACE_PERIOD` (oracle prices likely stale during
    ///      the grace window). No-op if `sequencerUptimeFeed` is unset.
    function _checkSequencer() internal view {
        if (sequencerUptimeFeed == address(0)) return;
        (, int256 answer, uint256 startedAt, , ) =
            IAggregatorV3(sequencerUptimeFeed).latestRoundData();
        // Chainlink L2 sequencer feeds report answer = 0 when up, 1 when down.
        require(answer == 0, "sequencer down");
        require(block.timestamp - startedAt >= SEQUENCER_GRACE_PERIOD, "sequencer grace");
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

    function setMaxSlippageBps(uint16 newValue) external onlyOwner {
        require(newValue <= 10000, "bps > max");
        maxSlippageBps = newValue;
        emit MaxSlippageBpsSet(newValue);
    }

    function setMaxPerCallLossBps(uint16 newValue) external onlyOwner {
        require(newValue <= 10000, "bps > max");
        maxPerCallLossBps = newValue;
        emit MaxPerCallLossBpsSet(newValue);
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

        uint256 taBefore = totalAssets();
        uint256 taPreCall = taBefore;

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

            uint256 taPostCall = totalAssets();
            if (taPreCall > 0) {
                uint256 minPostCall = taPreCall * (10000 - maxPerCallLossBps) / 10000;
                require(taPostCall >= minPostCall, "per-call loss");
            }
            taPreCall = taPostCall;
        }

        uint256 taAfter = taPreCall; // == totalAssets() after the last call
        if (taBefore > 0) {
            uint256 minAfter = taBefore * (10000 - maxSlippageBps) / 10000;
            require(taAfter >= minAfter, "max slippage");
        }
        require(taAfter >= minTotalAssetsAfter, "slippage");
        emit AllocationExecuted(decisionId, taAfter);
    }
}
