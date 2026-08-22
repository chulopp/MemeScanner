import pytest
from datetime import datetime, timedelta
from src.filters.funding_graph import (
    FundingGraphTracer,
    FundingTraceNode,
    FundingHopInfo,
    DisjointSet
)
from src.utils.known_wallets import KNOWN_CEX_WALLETS, is_known_cex_or_system


def test_disjoint_set_union_find():
    """Verify DisjointSet grouping logic."""
    dset = DisjointSet(["W1", "W2", "W3", "W4"])
    
    assert dset.find("W1") == "W1"
    assert dset.find("W2") == "W2"

    dset.union("W1", "W2")
    assert dset.find("W1") == dset.find("W2")

    dset.union("W2", "W3")
    assert dset.find("W1") == dset.find("W3")
    assert dset.find("W4") == "W4"


def test_known_cex_recognition():
    """Verify that known CEX wallets are correctly identified and excluded."""
    # Binance hot wallet
    binance_wallet = "5tzFkiKscBizbkMb2wvoebVKKnVeJijuJBCRggSpMtWC"
    assert is_known_cex_or_system(binance_wallet) is True

    # Random private Solana wallet
    private_wallet = "91PazXay3tZhwd3m6YY8LxK6i9DfDxWWBdJ1YocvyH1T"
    assert is_known_cex_or_system(private_wallet) is False


def test_cluster_detection_shared_hop1_funder():
    """Verify that two wallets funded by the same private parent wallet are clustered."""
    tracer = FundingGraphTracer()
    parent_funder = "ParentFunderWallet11111111111111111111111111"
    now = datetime.utcnow()

    node1 = FundingTraceNode(
        wallet_address="Wallet11111111111111111111111111111111111111",
        token_holding_amount=150_000_000.0,
        token_holding_pct=15.0,
        hop1=FundingHopInfo(
            funder_address=parent_funder,
            funding_timestamp=now,
            amount_sol=2.0,
            is_known_cex=False
        )
    )

    node2 = FundingTraceNode(
        wallet_address="Wallet22222222222222222222222222222222222222",
        token_holding_amount=150_000_000.0,
        token_holding_pct=15.0,
        hop1=FundingHopInfo(
            funder_address=parent_funder,
            funding_timestamp=now + timedelta(minutes=5),
            amount_sol=2.0,
            is_known_cex=False
        )
    )

    clusters, max_supply_pct, relationships = tracer.analyze_clusters([node1, node2])

    assert len(clusters) == 1
    assert clusters[0]["wallets_count"] == 2
    assert max_supply_pct == 30.0  # 15% + 15%
    assert len(relationships) == 1
    assert relationships[0]["relationship_type"] == "SHARED_FUNDER_HOP1"
    assert relationships[0]["shared_funder"] == parent_funder


def test_cex_funded_wallets_not_clustered():
    """Verify that two wallets funded by Binance Hot Wallet are NOT falsely clustered."""
    tracer = FundingGraphTracer()
    binance_wallet = "5tzFkiKscBizbkMb2wvoebVKKnVeJijuJBCRggSpMtWC"
    now = datetime.utcnow()

    node1 = FundingTraceNode(
        wallet_address="WalletA111111111111111111111111111111111111",
        token_holding_pct=10.0,
        hop1=FundingHopInfo(
            funder_address=binance_wallet,
            funding_timestamp=now,
            amount_sol=5.0,
            is_known_cex=True
        )
    )

    node2 = FundingTraceNode(
        wallet_address="WalletB222222222222222222222222222222222222",
        token_holding_pct=10.0,
        hop1=FundingHopInfo(
            funder_address=binance_wallet,
            funding_timestamp=now + timedelta(minutes=10),
            amount_sol=5.0,
            is_known_cex=True
        )
    )

    clusters, max_supply_pct, relationships = tracer.analyze_clusters([node1, node2])

    # Since funder is Binance CEX, no Sybil relationship should be formed
    assert len(relationships) == 0
    assert max_supply_pct == 0.0


def test_direct_funding_relationship():
    """Verify direct transfer between early buyer wallets is detected."""
    tracer = FundingGraphTracer()
    w1 = "Buyer111111111111111111111111111111111111111"
    w2 = "Buyer222222222222222222222222222222222222222"

    node1 = FundingTraceNode(
        wallet_address=w1,
        token_holding_pct=12.0,
        hop1=None
    )

    node2 = FundingTraceNode(
        wallet_address=w2,
        token_holding_pct=14.0,
        hop1=FundingHopInfo(
            funder_address=w1,
            funding_timestamp=datetime.utcnow(),
            amount_sol=1.5,
            is_known_cex=False
        )
    )

    clusters, max_supply_pct, relationships = tracer.analyze_clusters([node1, node2])

    assert len(relationships) == 1
    assert relationships[0]["relationship_type"] == "DIRECT_FUNDING"
    assert max_supply_pct == 26.0  # 12% + 14%
