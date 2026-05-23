// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {IStrategyAdapter} from "./IStrategyAdapter.sol";

/// @title BybitAttestor
/// @notice Off-chain Bybit balance represented as an IStrategyAdapter.
///         To CapitalManager this contract looks like any other whitelisted
///         adapter (`deposit` / `withdraw` / `valueInUsdc`). Under the hood
///         real capital lives off-chain on a Bybit account controlled by the
///         attestor (a Gnosis Safe). The attestor pushes signed
///         attestations of the Bybit-side USDC equivalent balance into this
///         contract.
///
///         Trust model — same pattern as Ondo USDY / BlackRock BUIDL:
///         vUSDC holders trust the attestor (Safe 2-of-3) to (a) honestly
///         report the off-chain balance and (b) actually return funds
///         on-chain when a withdraw is requested. In production this is
///         strengthened with Chainlink Proof of Reserves, multiple
///         attestors and an insurance fund; for the MVP it is a single
///         Safe with on-chain sanity checks (see subtasks .4 and .7) as
///         circuit breakers.
///
/// @dev    Oracle rules (per IStrategyAdapter NatSpec): the oracle for
///         `valueInUsdc()` is the attestor-pushed `attestedBalance`. This
///         is a deliberate deviation from manipulation-resistant on-chain
///         oracles: there is no on-chain price source for Bybit-side
///         positions. Liveness is enforced via `lastAttestationTime` — if
///         the attestor goes silent past a heartbeat, `valueInUsdc()`
///         will revert (implemented in subtask .8). Sanity bounds on
///         single-update balance moves are enforced in `confirmDeposit`,
///         `confirmWithdraw` and `updateBalance` (subtasks .4, .6, .7).
contract BybitAttestor is IStrategyAdapter, Ownable {
    using SafeERC20 for IERC20;

    IERC20  public immutable usdc;
    address public immutable vault;
    address public immutable attestor;

    /// @notice Authoritative Bybit-side USDC equivalent balance, last
    ///         pushed by the attestor. Denominated in USDC (6 decimals).
    ///         Updated by `confirmDeposit`, `confirmWithdraw` and
    ///         `updateBalance`.
    uint256 public attestedBalance;

    /// @notice USDC amounts escrowed on this contract awaiting the
    ///         off-chain bot to bridge them to Bybit and call
    ///         `confirmDeposit`. Keyed by txId.
    mapping(uint256 => uint256) public pendingDeposits;

    /// @notice Running sum of `pendingDeposits` across all open txIds.
    ///         Updated on `deposit` (++) and `confirmDeposit` (--).
    ///         Solidity mappings can't be iterated, so this is the only
    ///         way to expose the total to `valueInUsdc()` in O(1).
    uint256 public totalPendingDeposits;

    /// @notice USDC amounts the off-chain bot has been asked to bring
    ///         back from Bybit. Keyed by txId. Cleared on
    ///         `confirmWithdraw`.
    mapping(uint256 => uint256) public pendingWithdraws;

    /// @notice Monotonic counter used to assign txIds to deposit /
    ///         withdraw requests.
    uint256 public nextTxId;

    /// @notice Block timestamp of the last attestor push (any of
    ///         `confirmDeposit`, `confirmWithdraw`, `updateBalance`).
    ///         Drives staleness checks in `valueInUsdc()`.
    uint256 public lastAttestationTime;

    /// @notice Maximum age of `lastAttestationTime` before `valueInUsdc`
    ///         reverts as stale. Matches the operational threshold in the
    ///         epic Notes ("if > 24h без update, что-то пошло не так").
    uint256 public constant HEARTBEAT = 24 hours;

    /// @notice Emitted when CapitalManager calls `deposit`. The off-chain
    ///         bot listens for this event, pulls escrowed USDC, transfers
    ///         it to Bybit and subscribes the funds into a chosen Earn
    ///         product. The bot then closes the loop via `confirmDeposit`.
    event DepositRequested(uint256 indexed txId, uint256 amount);

    /// @notice Emitted by the attestor after off-chain capital has been
    ///         deployed on Bybit. Carries the new authoritative Bybit-side
    ///         USDC equivalent balance.
    event DepositConfirmed(uint256 indexed txId, uint256 newAttestedBalance);

    /// @notice Emitted when CapitalManager calls `withdraw`. The bot must
    ///         close hedges, redeem Earn positions, swap back to USDC and
    ///         bridge USDC from Bybit to Mantle. The withdraw is async —
    ///         `withdraw()` itself returns 0 and the actual USDC arrives
    ///         later via `confirmWithdraw`.
    event WithdrawRequested(uint256 indexed txId, uint256 amount);

    /// @notice Emitted by the attestor after USDC has been bridged back
    ///         on-chain and returned to CapitalManager.
    event WithdrawConfirmed(uint256 indexed txId, uint256 amount);

    /// @notice Emitted by the attestor on a periodic balance push (5 min
    ///         for volatile positions, 30 min for stable-only).
    event BalanceUpdated(uint256 newAttestedBalance, uint256 timestamp);

    modifier onlyVault() {
        require(msg.sender == vault, "not vault");
        _;
    }

    modifier onlyAttestor() {
        require(msg.sender == attestor, "not attestor");
        _;
    }

    constructor(
        address _usdc,
        address _vault,
        address _attestor,
        address _owner
    ) Ownable(_owner) {
        usdc     = IERC20(_usdc);
        vault    = _vault;
        attestor = _attestor;
    }

    /// @inheritdoc IStrategyAdapter
    function asset() external view returns (address) {
        return address(usdc);
    }

    /// @inheritdoc IStrategyAdapter
    /// @dev Pulls `amount` USDC from `vault` into escrow on this contract,
    ///      assigns a fresh `txId`, records `pendingDeposits[txId] = amount`
    ///      and emits `DepositRequested(txId, amount)`. The off-chain bot
    ///      listens for that event, pulls escrowed USDC out (subtask .4
    ///      `confirmDeposit`), bridges it to Bybit and stakes into an
    ///      Earn product.
    ///
    ///      Note: `IStrategyAdapter.deposit` has no return value, so the
    ///      assigned `txId` is observable only via the emitted event.
    ///      `CapitalManager` has no on-chain need for the id; the off-chain
    ///      bot reads it from event logs.
    function deposit(uint256 amount) external onlyVault {
        // `vault` is immutable and the call is gated by `onlyVault`, so the
        // safeTransferFrom source is bounded to the vault itself. Slither
        // false-positive otherwise (same pattern as AaveV3UsdcAdapter).
        // slither-disable-next-line arbitrary-send-erc20
        usdc.safeTransferFrom(vault, address(this), amount);

        uint256 txId = nextTxId++;
        pendingDeposits[txId] = amount;
        totalPendingDeposits += amount;
        emit DepositRequested(txId, amount);
    }

    /// @notice Called by the attestor after escrowed USDC has been moved
    ///         off-chain into Bybit. Clears the pending deposit, transfers
    ///         the escrowed USDC out to the attestor (Safe-controlled
    ///         deposit address), and records the new authoritative
    ///         Bybit-side balance.
    ///
    /// @dev    Sanity check (lower bound only):
    ///           newAttestedBalance >= attestedBalance + amount / 2
    ///         A fresh deposit of `amount` USDC should grow the Bybit-side
    ///         balance by roughly `amount`. Allowing up to 50% slippage
    ///         covers swap fees on USDC→target-asset conversions, Bybit
    ///         deposit fees and price slippage on volatile Earn products;
    ///         a larger drop indicates attestor compromise or a fat-finger
    ///         and is rejected. Upper bound is enforced separately in
    ///         `updateBalance` (subtask .7).
    ///
    ///         Reverts on:
    ///         - caller != attestor
    ///         - txId not in pendingDeposits (already confirmed, or never
    ///           requested)
    ///         - newAttestedBalance below the sanity lower bound
    function confirmDeposit(uint256 txId, uint256 newAttestedBalance) external onlyAttestor {
        uint256 amount = pendingDeposits[txId];
        require(amount > 0, "no pending deposit");
        require(
            newAttestedBalance >= attestedBalance + amount / 2,
            "attested balance below sanity floor"
        );

        delete pendingDeposits[txId];
        totalPendingDeposits -= amount;
        attestedBalance = newAttestedBalance;
        lastAttestationTime = block.timestamp;

        usdc.safeTransfer(attestor, amount);

        emit DepositConfirmed(txId, newAttestedBalance);
    }

    /// @inheritdoc IStrategyAdapter
    /// @dev Async — returns 0 immediately. Records a pending withdraw and
    ///      emits `WithdrawRequested(txId, amount)`. The off-chain bot
    ///      closes hedges, redeems Earn positions, swaps back to USDC,
    ///      bridges USDC from Bybit to Mantle, then calls `confirmWithdraw`
    ///      (subtask .6) to deliver the USDC back to the vault.
    ///
    ///      Implications for `CapitalManager`/`VUSDC.redeem`: since 0
    ///      USDC moves on-chain here, callers MUST treat this adapter's
    ///      `withdraw` as async. vUSDC redeems that would require pulling
    ///      from Bybit must revert with "insufficient onchain liquidity"
    ///      (see epic Notes — design choice for MVP, no queue mechanism).
    ///
    ///      `require(amount > 0)` is the only on-chain check. We do not
    ///      compare `amount` against `attestedBalance` here: that value
    ///      is stale by up to 5 minutes (per attestor push cadence) and
    ///      a transient bound check would fail on legitimate withdraws.
    ///      `CapitalManager` is responsible for sizing the request.
    function withdraw(uint256 amount) external onlyVault returns (uint256) {
        require(amount > 0, "amount = 0");

        uint256 txId = nextTxId++;
        pendingWithdraws[txId] = amount;
        emit WithdrawRequested(txId, amount);

        return 0;
    }

    /// @notice Called by the attestor after USDC has been bridged back
    ///         from Bybit to Mantle. Settles a pending withdraw and
    ///         forwards the delivered USDC to the vault.
    ///
    /// @dev    Flow: attestor → this contract → vault (two-hop, per
    ///         epic). The two-hop keeps every adapter USDC movement
    ///         routed through the adapter itself, which is consistent
    ///         with `deposit`/`confirmDeposit` semantics and makes
    ///         balance-of asserts straightforward in tests.
    ///
    ///         `amount` is the actual delivered USDC, which may differ
    ///         from the originally requested `pendingWithdraws[txId]`
    ///         because of swap slippage on the return path
    ///         (volatile asset → USDC) and Bybit's withdraw fee. A 50%
    ///         lower-bound sanity check rejects pathological deliveries
    ///         that indicate attestor compromise or a fat-finger; this
    ///         mirrors the lower bound in `confirmDeposit`.
    ///
    ///         `attestedBalance` is intentionally NOT updated here.
    ///         The off-chain bot pushes a fresh balance shortly after
    ///         via `updateBalance` (subtask .7). Between this call and
    ///         that next push, `valueInUsdc()` overstates the position
    ///         by approximately `amount` — acceptable staleness for the
    ///         MVP (single attestor, 5-min cadence) and corrected on
    ///         the next cron tick.
    ///
    ///         Reverts on:
    ///         - caller != attestor
    ///         - txId not in pendingWithdraws
    ///         - amount == 0
    ///         - amount below 50% of the originally requested withdraw
    /// @notice Periodic Bybit-side balance push from the attestor.
    ///         Cadence: every ~5 minutes when volatile positions are open,
    ///         every ~30 minutes for stable-only portfolios. May also be
    ///         called immediately after a large market move (>2% in a
    ///         minute) to keep `valueInUsdc()` from diverging.
    ///
    /// @dev    Sanity bounds (per subtask .7):
    ///           upper: newAttestedBalance <= prev * 110 / 100  (+10%)
    ///           lower: newAttestedBalance >= prev *  95 / 100  (-5%)
    ///         Asymmetric: a +10% jump in 5 minutes from steady-state
    ///         already signals attestor compromise or fat-finger (real
    ///         Earn yields don't grow that fast); a -5% drop is the upper
    ///         end of plausible market moves before manual intervention
    ///         should kick in. Both bounds are circuit breakers — when
    ///         tripped, no attestation goes through and the attestor must
    ///         resolve the discrepancy off-chain. The contract has no
    ///         owner-override (per epic), so a tripped breaker means
    ///         operations pause until the attestor pushes a value inside
    ///         the band.
    ///
    ///         Deposit / withdraw paths skip these bounds: they have
    ///         their own sanity checks (`confirmDeposit` lower bound;
    ///         `confirmWithdraw` doesn't update `attestedBalance`).
    ///
    ///         First-ever balance MUST be seeded via `confirmDeposit`.
    ///         `updateBalance` is steady-state only — calling it while
    ///         `attestedBalance == 0` reverts. Avoids a divide-by-zero
    ///         interpretation of "0 ± 10%" and forces the bootstrap path
    ///         through the deposit flow.
    ///
    ///         Reverts on:
    ///         - caller != attestor
    ///         - attestedBalance == 0 (no prior state to compare against)
    ///         - newAttestedBalance outside the [prev*95/100, prev*110/100] band
    function updateBalance(uint256 newAttestedBalance) external onlyAttestor {
        uint256 prev = attestedBalance;
        require(prev > 0, "no prior balance");

        uint256 upperBound = (prev * 110) / 100;
        uint256 lowerBound = (prev * 95)  / 100;
        require(newAttestedBalance <= upperBound, "balance > +10%");
        require(newAttestedBalance >= lowerBound, "balance < -5%");

        attestedBalance = newAttestedBalance;
        lastAttestationTime = block.timestamp;

        emit BalanceUpdated(newAttestedBalance, block.timestamp);
    }

    function confirmWithdraw(uint256 txId, uint256 amount) external onlyAttestor {
        uint256 expected = pendingWithdraws[txId];
        require(expected > 0, "no pending withdraw");
        require(amount > 0, "amount = 0");
        require(amount >= expected / 2, "delivered below sanity floor");

        delete pendingWithdraws[txId];
        lastAttestationTime = block.timestamp;

        // `attestor` is immutable and the call is gated by `onlyAttestor`,
        // so the safeTransferFrom source is bounded to the attestor itself
        // (which must have approved this contract first).
        // slither-disable-next-line arbitrary-send-erc20
        usdc.safeTransferFrom(attestor, address(this), amount);
        usdc.safeTransfer(vault, amount);

        emit WithdrawConfirmed(txId, amount);
    }

    /// @inheritdoc IStrategyAdapter
    /// @dev Native unit for this adapter is USDC (6 decimals), so this
    ///      returns the raw attestor-pushed Bybit-side balance.
    ///      `pendingDeposits` are intentionally NOT included here —
    ///      they're on-chain escrow, not part of the Bybit-side position.
    ///      Vault accounting must use `valueInUsdc()`.
    function balance() external view returns (uint256) {
        return attestedBalance;
    }

    /// @inheritdoc IStrategyAdapter
    /// @dev Returns `attestedBalance + totalPendingDeposits`:
    ///
    ///        - `attestedBalance` — what the attestor last reported
    ///          living on Bybit (subscribed in Earn products + open spot
    ///          positions priced at current USDC, ± perp hedge P&L).
    ///        - `totalPendingDeposits` — USDC escrowed on this contract
    ///          that the bot hasn't yet picked up. Still part of the
    ///          vault's position; not yet reflected in `attestedBalance`.
    ///
    ///      `pendingWithdraws` are NOT subtracted: `attestedBalance`
    ///      still includes them at this stage; the decrement happens on
    ///      the next `updateBalance` push after the bot redeems the
    ///      Bybit-side position.
    ///
    ///      Staleness: reverts when more than `HEARTBEAT` (24h) has
    ///      elapsed since `lastAttestationTime`. This satisfies the
    ///      liveness requirement in `IStrategyAdapter` NatSpec — vault
    ///      accounting (`CapitalManager.totalAssetsUsdc`) will propagate
    ///      the revert and pause mints/redeems until the attestor pushes
    ///      a fresh value.
    ///
    ///      `lastAttestationTime == 0` (no attestation ever made) is
    ///      treated as not-yet-stale rather than stale: the contract
    ///      may have escrowed pendingDeposits that aren't on Bybit yet,
    ///      and reporting their value as 0 would be wrong. Returns
    ///      `totalPendingDeposits` (== escrow balance) in that case.
    function valueInUsdc() external view returns (uint256) {
        if (lastAttestationTime != 0) {
            require(
                block.timestamp - lastAttestationTime <= HEARTBEAT,
                "attestation stale"
            );
        }
        return attestedBalance + totalPendingDeposits;
    }
}
