from pathlib import Path

from cofounder_kernel.authority import AuthorityDecision, AuthorityPolicy, AuthorityRequest


def test_authority_allows_local_reads_and_memory_writes(tmp_path: Path) -> None:
    policy = AuthorityPolicy(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data")

    read = policy.evaluate(AuthorityRequest(action="memory.search", permission_tier="L0_READ", target="memories"))
    write = policy.evaluate(AuthorityRequest(action="memory.write", permission_tier="L1_MEMORY_WRITE", target="memories"))
    ingest = policy.evaluate(AuthorityRequest(action="ingest.file", permission_tier="L1_MEMORY_WRITE", target=str(tmp_path)))

    assert read.decision == AuthorityDecision.ALLOW
    assert write.decision == AuthorityDecision.ALLOW
    assert ingest.decision == AuthorityDecision.ALLOW


def test_authority_requires_approval_for_generic_writes_and_external_actions(tmp_path: Path) -> None:
    policy = AuthorityPolicy(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data")

    file_write = policy.evaluate(
        AuthorityRequest(action="workspace.edit_file", permission_tier="L2_FILE_WRITE", target=str(tmp_path / "app.py"))
    )
    external = policy.evaluate(
        AuthorityRequest(action="email.send", permission_tier="L3_EXTERNAL_ACTION", target="outbound email")
    )

    assert file_write.decision == AuthorityDecision.APPROVAL_REQUIRED
    assert file_write.requires_typed_phrase is True
    assert external.decision == AuthorityDecision.APPROVAL_REQUIRED


def test_authority_denies_live_trading_and_destructive_actions(tmp_path: Path) -> None:
    policy = AuthorityPolicy(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data")

    trade = policy.evaluate(
        AuthorityRequest(action="broker.place_order", permission_tier="L3_EXTERNAL_ACTION", target="live trade")
    )
    destructive = policy.evaluate(
        AuthorityRequest(
            action="shell.run",
            permission_tier="L3_EXTERNAL_ACTION",
            target="format disk",
            metadata={"command": "format disk"},
        )
    )

    assert trade.decision == AuthorityDecision.DENY
    assert destructive.decision == AuthorityDecision.DENY


def test_authority_does_not_block_memory_about_sensitive_topics(tmp_path: Path) -> None:
    policy = AuthorityPolicy(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data")

    result = policy.evaluate(
        AuthorityRequest(
            action="memory.write",
            permission_tier="L1_MEMORY_WRITE",
            target="memories",
            metadata={"content": "Notes about broker boundaries are evidence, not execution."},
        )
    )

    assert result.decision == AuthorityDecision.ALLOW
