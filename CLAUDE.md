# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Python CLI tool for MercadoLibre (Argentine marketplace) sellers that automates competitive analysis, pricing, listing optimization, and account management. Integrates with the MercadoLibre OAuth API and Claude AI (Anthropic) for intelligent recommendations.

## Setup & Running

```bash
pip install -r requirements.txt
python main.py                          # Show help and all commands
```

There is no test suite, Makefile, or build system. All testing is manual via CLI commands.

## Common Commands

**Account management:**
```bash
python main.py agregar          # Add account (interactive OAuth flow)
python main.py listar           # List configured accounts
python main.py verificar        # Test connection to all accounts
python main.py eliminar         # Remove account
```

**Resumen y tendencias:**
```bash
python main.py dashboard                              # Vista de salud de todas las cuentas
python main.py historial [alias]                      # Tendencias de reputación + posiciones
python main.py historial [alias] reputacion           # Solo historial de reputación
python main.py historial [alias] posiciones [dias]    # Solo posiciones, N días (default 7)
```

**Feature modules:**
```bash
python main.py posiciones [alias]              # Module 1 — position tracking
python main.py competencia [alias] [max_cats]  # Module 2 — competitor analysis
python main.py optimizar [alias] [max_items]   # Module 3 — AI listing optimizer (requires Module 2 data)
python main.py lanzar "product idea"           # Module 4 — launch planner
python main.py repricing-setup [alias]         # Module 5 — configure pricing rules
python main.py repricing [alias] [--apply]     # Module 5 — dry-run or apply repricing
python main.py preguntas [alias] [--auto]      # Module 6 — answer questions
python main.py reputacion [alias]              # Module 6 — reputation metrics
python main.py costos [alias]                  # Module 7 — cargar costos por producto
python main.py stock-rentabilidad [alias]      # Module 7 — stock, velocidad de ventas y margen
python main.py stock-rentabilidad [alias] --todos  # Module 7 — mostrar todas (no solo alertas)
python main.py radar [alias] [radar|calendario]    # Module 8 — radar de nichos + calendario
python main.py full-setup [alias]              # Module 9 — configurar lead times Full
python main.py full [alias]                    # Module 9 — gestión Mercado Envíos Full
python main.py reposicion-setup [alias]        # Module 10 — configurar tránsito y depósito
python main.py reposicion [alias]              # Module 10 — plan de reposición desde China
python main.py multicuenta [resumen|cruzada]  # Module 11 — panel multicuenta e inteligencia cruzada
python main.py todo                            # Run Modules 1+2 across all accounts
```

## Architecture

### Layers

**`core/`** — Foundation shared by all modules:
- `models.py` — `MLAccount` dataclass with token auto-refresh logic (refreshes if <5min remaining)
- `ml_client.py` — `requests.Session` wrapper for MercadoLibre API; auto-refreshes on 401; callback persists updated tokens
- `account_manager.py` — Loads/saves up to 4 accounts from `config/accounts.json`; `get_client(alias)` returns an `MLClient`

**`modules/`** — 7 independent feature modules, each with a `run()` entry point. Each module receives an `MLClient` instance and account alias.

**`main.py`** — Rich-based CLI dispatcher: parses `sys.argv`, resolves account(s), calls module `run()`.

### Module Dependency
Module 3 (`optimizar`) reads the JSON report produced by Module 2 (`competencia`). All other modules are independent.

### Data Persistence
- `config/accounts.json` — OAuth credentials and tokens (git-ignored)
- `config/repricing.json` — Pricing rules per item (created by Module 5 setup)
- `config/costos.json` — Item costs for margin calculation (created by Module 7)
- `data/posiciones_<Alias>.json` — Daily position snapshots
- `data/competencia_<Alias>.json` — Competitive analysis reports (input to Module 3)
- `data/optimizaciones_<Alias>.json` — Before/after optimization history
- `data/reputacion_<Alias>.json` — Rolling 30-snapshot reputation history
- `data/stock_<Alias>.json` — Stock and profitability snapshots
- `data/lanzamiento_<product>_<timestamp>.json` — Launch analysis outputs

### MercadoLibre API
- Base: `https://api.mercadolibre.com`
- Auth: Bearer token (OAuth 2.0 with refresh)
- Site: `MLA` (Argentina)
- Key permission: `Ítems y búsqueda` enables direct `/sites/MLA/search`; Module 2 falls back to catalog search if unavailable
- Modules add `time.sleep(0.1–0.3)` between calls to avoid throttling

### Claude AI Integration
- Model: `claude-opus-4-6` with adaptive thinking and streaming
- Used by Modules 3 (optimize listings), 4 (launch planner), 6 (answer questions)
- Token budgets: 256 tokens (Q&A), 2048 (listing optimizer), 4096 (launch planner)
- Module 3 parses structured response: `TÍTULO: [...] --- DESCRIPCIÓN: [...]`

### Business Logic Constants
- ML commission rates: 13% (classic listing), 16.5% (premium listing)
- Estimated shipping cost: $700 ARS (when free shipping is enabled)
- Repricing rules: beat competitor by 1% (`competitor × 0.99`), raise 2% when no competitor, never go below configured `precio_min`
- Position alert threshold: drop >3 places triggers warning
- Reputation thresholds (reclamos): ≤1% green, ≤2% yellow, >2% red
