// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

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

/// @notice Raw capital pool: holds USDC + manages adapter positions.
/// @dev Not ERC-4626. The user-facing yield-bearing token is vUSDC, which
///      sits on top and is the only contract permitted to call
///      `recordDeposit` / `recordWithdraw`.
contract CapitalManager is Ownable, Pausable, ReentrancyGuard {
    using SafeERC20 for IERC20;
    using EnumerableSet for EnumerableSet.AddressSet;

    enum AllocationCallKind { Deposit, Withdraw }

    struct AllocationCall {
        address adapter;
        AllocationCallKind kind;
        uint256 amount;
    }

    /// @notice Grace period after a sequencer restart during which oracle prices
    ///         may still be stale and `totalAssetsUsdc()` will revert.
    uint256 public constant SEQUENCER_GRACE_PERIOD = 1 hours;

    /// @notice USDC token — the manager's base asset (6 decimals).
    IERC20 public immutable usdc;

    /// @notice Chainlink L2 Sequencer Uptime Feed. Zero address disables the check
    ///         (used when no feed is available on the target L2 yet).
    address public immutable sequencerUptimeFeed;

    /// @notice vUSDC token contract — the sole caller permitted to record
    ///         user-facing deposits/withdrawals. Set once by owner via
    ///         `setVusdc()` after deploy (immutable-after-set). Not set in
    ///         constructor to avoid a deploy-time circular dependency with
    ///         vUSDC (which references CapitalManager in its own constructor).
    address public vusdc;

    /// @notice Owner-set cap on total slippage per `executeAllocation`. Basis
    ///         points of `totalAssetsUsdc()` measured before vs. after the batch.
    ///         Default 100 (1%). Protects against compromised-agent scenarios
    ///         even when the agent passes `minTotalAssetsAfter = 0`.
    uint16 public maxSlippageBps;

    /// @notice Owner-set cap on slippage of any single call within a batch,
    ///         measured against `totalAssetsUsdc()` immediately before that call.
    ///         Default 100 (1%). Catches a single bad-oracle adapter even when
    ///         the global cap would otherwise be satisfied by gains elsewhere
    ///         in the batch.
    uint16 public maxPerCallLossBps;

    address public agent;
    EnumerableSet.AddressSet private _whitelisted;

    event VusdcSet(address indexed vusdc);
    event AgentSet(address indexed agent);
    event StrategyWhitelisted(address indexed strategy, bool status);
    event MaxSlippageBpsSet(uint16 newValue);
    event MaxPerCallLossBpsSet(uint16 newValue);
    event DepositRecorded(uint256 usdcAmount);
    event WithdrawRecorded(uint256 usdcAmount, address indexed to);
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

    modifier onlyVusdc() {
        require(msg.sender == vusdc, "not vusdc");
        _;
    }

    modifier onlyAgent() {
        require(msg.sender == agent, "not agent");
        _;
    }

    constructor(
        IERC20 _usdc,
        address _owner,
        address _sequencerUptimeFeed
    ) Ownable(_owner) {
        require(address(_usdc) != address(0), "zero usdc");
        usdc                = _usdc;
        sequencerUptimeFeed = _sequencerUptimeFeed;
        maxSlippageBps      = 100; // 1%
        maxPerCallLossBps   = 100; // 1%
    }

    // ─── vUSDC binding ───────────────────────────────────────────────────────

    /// @notice One-shot setter for the vUSDC token contract. Required because
    ///         the constructor cannot take vUSDC's address (vUSDC is deployed
    ///         after CapitalManager and references the manager itself).
    function setVusdc(address _vusdc) external onlyOwner {
        require(vusdc == address(0), "vusdc set");
        require(_vusdc != address(0), "zero vusdc");
        vusdc = _vusdc;
        emit VusdcSet(_vusdc);
    }

    // ─── vUSDC-only entry points ─────────────────────────────────────────────

    /// @notice Pull `usdcAmount` USDC from the vUSDC token contract into the
    ///         manager. Called by vUSDC when a user mints vUSDC shares.
    /// @dev    vUSDC MUST hold the USDC and have approved `usdcAmount` to
    ///         this contract before calling.
    function recordDeposit(uint256 usdcAmount)
        external
        nonReentrant
        whenNotPaused
        onlyVusdc
    {
        require(usdcAmount > 0, "zero amount");
        usdc.safeTransferFrom(msg.sender, address(this), usdcAmount);
        emit DepositRecorded(usdcAmount);
    }

    /// @notice Transfer `usdcAmount` USDC from the manager to `to`. Called by
    ///         vUSDC when a user redeems vUSDC shares back to USDC.
    /// @dev    Reverts if cash buffer is insufficient. Off-chain orchestration
    ///         (agent rebalance) must free enough cash before redemption.
    function recordWithdraw(uint256 usdcAmount, address to)
        external
        nonReentrant
        whenNotPaused
        onlyVusdc
    {
        require(to != address(0), "zero to");
        require(usdcAmount > 0, "zero amount");
        usdc.safeTransfer(to, usdcAmount);
        emit WithdrawRecorded(usdcAmount, to);
    }

    // ─── Total assets in USDC ────────────────────────────────────────────────

    /// @notice Total assets under management priced in USDC units (6 decimals).
    /// @dev Sums `valueInUsdc()` across every whitelisted adapter plus the
    ///      free USDC balance held directly by the manager.
    ///
    ///      Fail-loud semantics: if ANY adapter's `valueInUsdc()` reverts
    ///      (e.g. its oracle is stale per the rules in IStrategyAdapter), the
    ///      entire call reverts. vUSDC reads this view for exchange-rate
    ///      calculations, so the revert propagates and blocks user-facing
    ///      mints/redeems until the offending adapter is de-whitelisted via
    ///      `whitelistStrategy(adapter, false)`. This is intentional —
    ///      silently undervaluing a position would dilute existing holders.
    ///      Operator procedure during a degraded oracle:
    ///        1. `pause()` the manager to halt vUSDC-initiated cash flow;
    ///        2. de-whitelist the broken adapter;
    ///        3. resume.
    ///      Owner can also exit funds via `emergencyWithdraw(adapter)` which
    ///      bypasses `totalAssetsUsdc()`.
    function totalAssetsUsdc() public view returns (uint256) {
        _checkSequencer();
        uint256 sum = usdc.balanceOf(address(this));
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
    ///                            post-execution `totalAssetsUsdc()` falls below this.
    function executeAllocation(
        bytes32 decisionId,
        AllocationCall[] calldata calls,
        uint256 minTotalAssetsAfter
    ) external nonReentrant whenNotPaused onlyAgent {
        require(calls.length > 0, "empty calls");

        uint256 taBefore = totalAssetsUsdc();
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

            uint256 taPostCall = totalAssetsUsdc();
            if (taPreCall > 0) {
                uint256 minPostCall = taPreCall * (10000 - maxPerCallLossBps) / 10000;
                require(taPostCall >= minPostCall, "per-call loss");
            }
            taPreCall = taPostCall;
        }

        uint256 taAfter = taPreCall; // == totalAssetsUsdc() after the last call
        if (taBefore > 0) {
            uint256 minAfter = taBefore * (10000 - maxSlippageBps) / 10000;
            require(taAfter >= minAfter, "max slippage");
        }
        require(taAfter >= minTotalAssetsAfter, "slippage");
        emit AllocationExecuted(decisionId, taAfter);
    }
}
