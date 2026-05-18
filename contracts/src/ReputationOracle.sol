// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

interface IVault4626 {
    function totalAssets() external view returns (uint256);
}

interface IReputationRegistry {
    function giveFeedback(
        uint256 agentId,
        int128 value,
        uint8 valueDecimals,
        string calldata tag1,
        string calldata tag2,
        string calldata endpoint,
        string calldata feedbackURI,
        bytes32 feedbackHash
    ) external;
}

/// @notice Pull-based reputation oracle. Anyone calls updateReputation(); the
///         contract reads vault.totalAssets(), computes cumulative annualized
///         APR in signed bps, and pushes feedback to the canonical ERC-8004 Registry.
contract ReputationOracle {
    // ─── Constants ───────────────────────────────────────────────────────────

    uint256 public constant MIN_INTERVAL     = 1 hours;
    uint256 public constant SECONDS_PER_YEAR = 365 days;
    uint256 public constant BPS_SCALE        = 10_000;
    uint8   public constant VALUE_DECIMALS   = 2;

    // ─── Immutables ──────────────────────────────────────────────────────────

    IVault4626          public immutable vault;
    IReputationRegistry public immutable registry;
    uint256             public immutable agentId;

    // ─── State ───────────────────────────────────────────────────────────────

    uint256 public baselineAssets;
    uint256 public baselineTimestamp;
    uint256 public lastUpdateTimestamp;
    int128  public lastScore;
    uint64  public updateCount;

    // ─── Errors ──────────────────────────────────────────────────────────────

    error VaultEmpty();
    error TooSoon(uint256 nextAllowedAt);
    error ElapsedZero();
    error ScoreOverflow();
    error ZeroAddress();

    // ─── Events ──────────────────────────────────────────────────────────────

    event BaselineSet(uint256 assets, uint256 timestamp);
    event ReputationUpdated(
        uint64 indexed updateIndex,
        address indexed caller,
        uint256 currentAssets,
        int128 scoreBps,
        uint256 elapsedSeconds
    );

    // ─── Constructor ─────────────────────────────────────────────────────────

    constructor(address _vault, address _registry, uint256 _agentId) {
        if (_vault    == address(0)) revert ZeroAddress();
        if (_registry == address(0)) revert ZeroAddress();
        vault    = IVault4626(_vault);
        registry = IReputationRegistry(_registry);
        agentId  = _agentId;
    }

    // ─── External ────────────────────────────────────────────────────────────

    /// @notice Record cumulative APR to the ERC-8004 Registry. Callable by anyone.
    /// @return scoreBps Signed APR in basis points (VALUE_DECIMALS=2, so 1234 = 12.34%).
    function updateReputation() external returns (int128 scoreBps) {
        uint256 currentAssets = vault.totalAssets();
        if (currentAssets == 0) revert VaultEmpty();

        // First call after first deposit: set baseline and push a "started" feedback.
        if (baselineAssets == 0) {
            baselineAssets      = currentAssets;
            baselineTimestamp   = block.timestamp;
            lastUpdateTimestamp = block.timestamp;
            updateCount++;

            registry.giveFeedback(agentId, 0, VALUE_DECIMALS, "apr", "cumulative", "", "", bytes32(0));
            emit BaselineSet(currentAssets, block.timestamp);
            return 0;
        }

        uint256 next = lastUpdateTimestamp + MIN_INTERVAL;
        if (block.timestamp < next) revert TooSoon(next);

        uint256 elapsed = block.timestamp - baselineTimestamp;
        if (elapsed == 0) revert ElapsedZero();

        scoreBps            = _computeScore(currentAssets, elapsed);
        lastScore           = scoreBps;
        lastUpdateTimestamp = block.timestamp;
        uint64 idx          = ++updateCount;

        registry.giveFeedback(agentId, scoreBps, VALUE_DECIMALS, "apr", "cumulative", "", "", bytes32(0));
        emit ReputationUpdated(idx, msg.sender, currentAssets, scoreBps, elapsed);
    }

    /// @notice Preview what updateReputation would return right now without writing state.
    function previewScore()
        external
        view
        returns (int128 score, uint256 currentAssets, uint256 elapsed)
    {
        currentAssets = vault.totalAssets();
        if (currentAssets == 0 || baselineAssets == 0) return (0, currentAssets, 0);
        elapsed = block.timestamp - baselineTimestamp;
        if (elapsed == 0) return (0, currentAssets, 0);
        score = _computeScore(currentAssets, elapsed);
    }

    /// @notice True when a call to updateReputation() would not revert (ignoring ScoreOverflow).
    function canUpdate() external view returns (bool) {
        uint256 currentAssets = vault.totalAssets();
        if (currentAssets == 0) return false;
        if (baselineAssets == 0) return true;
        return block.timestamp >= lastUpdateTimestamp + MIN_INTERVAL;
    }

    // ─── Internal ────────────────────────────────────────────────────────────

    function _computeScore(uint256 currentAssets, uint256 elapsed) internal view returns (int128) {
        bool positive = currentAssets >= baselineAssets;
        uint256 diff  = positive ? currentAssets - baselineAssets : baselineAssets - currentAssets;

        // Multiply before divide to preserve precision.
        // For realistic asset values (< 1e50) this never overflows uint256.
        uint256 bps = diff * BPS_SCALE * SECONDS_PER_YEAR / (baselineAssets * elapsed);

        if (bps > uint256(uint128(type(int128).max))) revert ScoreOverflow();

        return positive ? int128(int256(bps)) : -int128(int256(bps));
    }
}
