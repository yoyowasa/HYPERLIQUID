# Hyperliquid

Integration tests connect to the Hyperliquid API over HTTPS and WebSockets.
To verify the SSL certificates presented by the servers, the tests rely on
[`certifi`](https://pypi.org/project/certifi/) for a trusted Certificate
Authority bundle. Ensure `certifi` is installed so these connections can be
established securely.

## Run PFPL on Testnet

Prerequisites:

- Create a `.env` with your testnet credentials
  - `HL_PRIVATE_KEY=<your testnet private key>`
  - `HL_ACCOUNT_ADDRESS=<your testnet account address>`
  - Optional: `LOG_LEVEL=DEBUG`

Quick start (Windows PowerShell):

- Dry-run (no real orders):
  - `scripts\start_pfpl_testnet.ps1 -Symbols "ETH-PERP" -OrderUsd 10 -DryRun`
- Live on testnet (orders go to testnet):
  - `scripts\start_pfpl_testnet.ps1 -Symbols "ETH-PERP" -OrderUsd 10`

The script will:

- Activate `.venv` if present
- Load `.env` using `python -m dotenv run`
- Start PFPL on the Hyperliquid testnet endpoint
- Retry on transient failures (5s backoff)

Logs:

- PFPL strategy logs per symbol are written to `logs/pfpl/<SYMBOL>.csv` with daily rotation.


