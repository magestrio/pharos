# Vault8004 — Project Context

## Project

Vault8004 — AI yield vault на Mantle. Агент Claude Opus 4.7 ребалансирует mETH/cmETH/sUSDe/Lendle USDC каждые 4 часа. Каждое решение on-chain + IPFS rationale. Репутация через ERC-8004 pull-based oracle. Хакатон Mantle Turing Test 2026, дедлайн 16 июня.

## Structure

- `/contracts` — Foundry, Solidity 0.8.24
- `/agent` — Python 3.11, Claude Opus 4.7 + cron loop
- `/web` — Next.js 14 + wagmi v2
- `/packages/abi` — shared ABI пакет, генерится из `contracts/out/`

## Conventions

- Solidity: 0.8.24, OpenZeppelin 5.x, Solady. БЕЗ upgradeable proxy, БЕЗ delegatecall.
- Python: pydantic для всех структур, deterministic validator ПОСЛЕ LLM (LLM не доверяем).
- Web: server components по дефолту, client только где wagmi hooks.
- `.env` никогда не коммитим, `.env.example` всегда обновляем.

## Hard caps для агента (deterministic validation)

- `sum(allocation) == 1.0 ± 0.001`
- cash buffer ≥ 3%
- любая позиция ≤ 60%
- sUSDe ≤ 50%
- confidence ≥ 0.4
- red risk flags → skip cycle
- 7-day avg funding sUSDe < 0 → mandatory exit

## Key addresses (заполнить после деплоя)

- ERC-8004 Reputation Registry (Mantle): `0x8004BAa17C...` (verify)
- Vault8004: TBD
- DecisionLog: TBD
- ReputationOracle: TBD

## Workflows

- Изменил контракт: `cd contracts && forge build && node scripts/export-abi.js` → ABI попадает в `@vault8004/abi`
- Запуск агента: `cd agent && uv run python -m agent.main`
- Запуск фронта: `pnpm --filter web dev`

## НЕ делать

- Не открывать публичные deposits (юр.риск)
- Не писать свой Reputation Registry — canonical 8004
- Не делать upgradeable vault
- Не подгонять prompt под backtest
- Не интегрировать больше 4 адаптеров
