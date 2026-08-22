# 🚀 MemeScanner

**MemeScanner** is a high-performance, real-time Solana meme coin safety scanner, data ingestion pipeline, and signal detection engine.

---

## 📌 Architecture & Features (Phase 0 & 1 Complete)

- **Real-Time Data Ingestion**:
  - **Pump.fun**: WebSocket listener via PumpPortal for instant bonding curve creations.
  - **Raydium AMM**: WebSocket listener via Helius RPC `logsSubscribe` for new pool initializations.
  - **Deduplication**: Atomic Redis `SETNX` cache (Upstash REST API) with resilient in-memory fallback.

- **Hard Safety Filter Pipeline (Ponyin Rules)**:
  - **Dev Allocation Check**: Flags tokens where initial dev allocation exceeds threshold (default `<= 10%`).
  - **Deployer History**: Cross-references deployer wallet reputation and past rug count in database.
  - **Instant Scalp Detection**:
    - Gas / Priority Fee Spikes (comparing against live network median).
    - Deployer wallet age verification via on-chain `getSignaturesForAddress`.
    - Deployer balance check.
    - Initial liquidity anomalies.
  - **Raydium Checks**: Mint authority & freeze authority renouncement validation via SPL Token parser.

- **Database & Security**:
  - **Supabase (PostgreSQL)**: Full 11-table schema with Row Level Security (RLS) enabled.
  - **Secure Backend Access**: Operations authenticated using `service_role` key.
  - **Input Sanitization**: Base58 address validation on all inbound events.

---

## 📂 Project Structure

```text
MemeScanner/
├── src/
│   ├── config.py              # Pydantic Settings & environment configuration
│   ├── main.py                # Main application orchestrator & CLI
│   ├── database/
│   │   ├── client.py          # Supabase DatabaseManager with in-memory fallback
│   │   └── models.py          # Pydantic models matching ERD tables
│   ├── filters/
│   │   ├── instant_scalp.py   # Ponyin instant scalp heuristics
│   │   ├── pipeline.py        # Central safety filter pipeline & rate limiting
│   │   ├── pump_safety.py     # Pump.fun hard safety filters
│   │   ├── raydium_safety.py  # Raydium LP & mint safety filters
│   │   └── schemas.py         # Filter result schemas
│   ├── ingestion/
│   │   ├── manager.py         # Ingestion manager coordinating listeners
│   │   ├── pumpportal_ws.py   # PumpPortal WebSocket listener
│   │   ├── raydium_ws.py      # Raydium Helius WebSocket listener
│   │   └── schemas.py         # RawTokenEvent schemas with Base58 validation
│   └── utils/
│       ├── logger.py          # Rich console logger with URL/API key masking
│       ├── redis_client.py    # Upstash Redis async manager
│       └── solana_rpc.py      # Helius RPC client with concurrency semaphore
├── tests/                     # Unit and integration test suite
├── PRDERD.md                  # Single source of truth (PRD + ERD documentation)
├── requirements.txt           # Python dependencies
├── .env.example               # Environment template
└── .gitignore                 # Protected secret filters
```

---

## 🛠️ Setup & Installation

### 1. Clone & Setup Virtual Environment
```bash
git clone git@github.com:chulopp/MemeScanner.git
cd MemeScanner
python -m venv venv
# On Windows PowerShell:
.\venv\Scripts\Activate.ps1
# On Linux / WSL / macOS:
source venv/bin/activate
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables
Copy `.env.example` to `.env` and fill in your credentials:
```bash
cp .env.example .env
```

Required keys:
- `HELIUS_API_KEY`: Helius RPC API key.
- `UPSTASH_REDIS_REST_URL` & `UPSTASH_REDIS_REST_TOKEN`: Upstash Redis REST credentials.
- `SUPABASE_URL` & `SUPABASE_SERVICE_KEY`: Supabase project URL and service role secret key.

---

## 🧪 Testing & Execution

### Run Test Suite
```bash
python -m pytest -v
```

### Run Live Scanner
```bash
# Live continuous scanning:
python -m src.main

# Smoke test mode (e.g. 30 seconds):
python -m src.main --smoke-test --duration 30
```

---

## 📜 License
MIT
