"""
Known CEX Hot Wallets, Bridges, and Solana System Addresses.
Used to filter out shared parent funding false-positives (e.g. two independent users withdrawing from Binance).
All addresses are verified against on-chain Solscan records.
"""

from typing import Set

# Known CEX Hot Wallets on Solana (Verified on-chain)
KNOWN_CEX_WALLETS: Set[str] = {
    # Binance Hot Wallets
    "5tzFkiKscBizbkMb2wvoebVKKnVeJijuJBCRggSpMtWC",
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
    "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S",
    "3yFwqXBfZY4jBVUafQ182cLVYTrTapTYuMChTckMsU5x",

    # Coinbase Hot Wallets
    "2AQdpHJ2JpcEgBtAZUXpqkWwdDTsqTQjM4Mt5xuh5BpU",
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS",
    "GJRs4FwHtemZ5ZE9x3FNvJ8TMwitKTh21yxdRPqn7npE",

    # OKX Hot Wallets
    "5VCwKtCXgCJ6kit5FybXjvmsWnGnCghND9stb5K7Dwin",
    "6ZRCB7AAqGre6c72PRz3MHLC73VMYvJ8bi9KHf1DTUpk",

    # Bybit Hot Wallets
    "AC5RDfQFmDS1deWZos921qqvw3xEmMmNtx9UJWNumwEW",
    "9uyDbBPrLddkFv34w7r7T1WfXo1S4iN2PZ3YgVdD1Hn",

    # KuCoin Hot Wallet
    "BmFdpraQhkiDQE6f4hyReAQnsBgTx2osACxRydNqYgN",

    # Kraken Hot Wallet
    "FWznbcNXWQuHTawe9RxvQ2LdJF4PqL4kK1qQZ4c9rN1",

    # MEXC Hot Wallet
    "ASTyfSima4LLAdDgoFGkgqoKowG1LZFDr9fAQrg7iaJZ",

    # Gate.io Hot Wallet
    "u6PJ8DtQuPFnfmwHbGFULQ4u4Egsv226CW7zZzk7Ntw",
}

# Known System & Protocol Infrastructure Addresses
KNOWN_SYSTEM_PROGRAMS: Set[str] = {
    "11111111111111111111111111111111",              # System Program
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL Token Program
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022 Program
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",  # Associated Token Program
    "So11111111111111111111111111111111111111112",  # Wrapped SOL
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM V4
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",  # Raydium CPMM
    "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX",  # OpenBook V1
    "opnb2ScanFZmgcc2tguaPxd7wJUzUCbvFCwFHNPDRWA",  # OpenBook V2
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # Pump.fun Program
    "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM",  # Pump.fun Fee Account
    "ComputeBudget111111111111111111111111111111",  # Compute Budget Program
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",  # Memo Program
}


def is_known_cex_or_system(address: str) -> bool:
    """Returns True if address belongs to a known CEX Hot Wallet or Solana System/Protocol program."""
    if not address:
        return False
    return address in KNOWN_CEX_WALLETS or address in KNOWN_SYSTEM_PROGRAMS
