from morphlake.partitioning import domain_shard


def test_domain_shard_is_stable_and_bounded():
    first = domain_shard("risk", 32)
    assert first == domain_shard("risk", 32)
    assert 0 <= first < 32


def test_domain_shard_rejects_invalid_count():
    try:
        domain_shard("risk", 0)
    except ValueError as exc:
        assert "positive" in str(exc)
    else:
        raise AssertionError("expected ValueError")
