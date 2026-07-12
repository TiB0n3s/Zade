from pathlib import Path

from fastapi.testclient import TestClient

from cofounder_kernel.api import create_app
from cofounder_kernel.authority import AuthorityDecision, AuthorityPolicy, AuthorityRequest
from cofounder_kernel.config import AppConfig, KernelConfig, OllamaConfig, PathConfig
from cofounder_kernel.ollama import OllamaClient


def fake_health(self: OllamaClient) -> dict:
    return {"version": "test"}


def make_policy(tmp_path: Path) -> AuthorityPolicy:
    return AuthorityPolicy(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data")


def evaluate(policy: AuthorityPolicy, action: str, tier: str, target: str = "", metadata: dict | None = None):
    return policy.evaluate(
        AuthorityRequest(action=action, permission_tier=tier, target=target, metadata=metadata or {})
    )


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


def test_metadata_content_never_changes_a_decision(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    spicy_metadata = {
        "note": "reviewed the purchase order and broker analysis",
        "content": "wire transfer fraud writeup; recommends disable firewall audits",
    }

    # An approved local handler item stays approvable even when its payload
    # mentions denied capabilities (the old engine denied this at queue time).
    report = evaluate(policy, "local.report.write", "L3_EXTERNAL_ACTION", target="local_report", metadata=spicy_metadata)
    # A local read stays autonomous regardless of what the payload discusses.
    summary = evaluate(policy, "notes.summarize", "L0_READ", metadata={"note": "wire transfer to broker"})
    # A local memory write with a spicy payload stays autonomous.
    review = evaluate(policy, "goal.review", "L1_MEMORY_WRITE", metadata=spicy_metadata)

    assert report.decision == AuthorityDecision.APPROVAL_REQUIRED
    assert summary.decision == AuthorityDecision.ALLOW
    assert review.decision == AuthorityDecision.ALLOW


def test_claimed_tier_cannot_bypass_the_deny_boundary(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)

    # The old engine allowed anything claiming L0_READ without a deny check.
    broker_l0 = evaluate(policy, "broker.place_order", "L0_READ", target="TSLA")
    trading_l1 = evaluate(policy, "trading.execute", "L1_MEMORY_WRITE")
    payment_l3 = evaluate(policy, "payment.send", "L3_EXTERNAL_ACTION")
    wire_l2 = evaluate(policy, "wire.transfer", "L2_FILE_WRITE")

    assert broker_l0.decision == AuthorityDecision.DENY
    assert broker_l0.matched_rule == "deny.action_token"
    assert trading_l1.decision == AuthorityDecision.DENY
    assert payment_l3.decision == AuthorityDecision.DENY
    assert wire_l2.decision == AuthorityDecision.DENY


def test_external_actions_cannot_claim_read_tier(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)

    email_l0 = evaluate(policy, "email.send", "L0_READ", target="founder@example.com")
    shell_l0 = evaluate(policy, "shell.exec", "L0_READ")
    deploy_l1 = evaluate(policy, "deploy.site", "L1_MEMORY_WRITE")

    assert email_l0.decision == AuthorityDecision.APPROVAL_REQUIRED
    assert email_l0.matched_rule == "approval.action_token"
    assert email_l0.requires_typed_phrase is True
    assert shell_l0.decision == AuthorityDecision.APPROVAL_REQUIRED
    assert deploy_l1.decision == AuthorityDecision.APPROVAL_REQUIRED


def test_local_files_with_spicy_names_still_ingest(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)

    purchase_file = evaluate(
        policy,
        "ingest.file",
        "L1_MEMORY_WRITE",
        target=r"C:\AI Brain\inbox\purchase-order-notes.md",
    )
    firewall_file = evaluate(
        policy,
        "ingest.file",
        "L1_MEMORY_WRITE",
        target=r"C:\AI Brain\inbox\how-to-disable-firewall-safely.md",
    )

    assert purchase_file.decision == AuthorityDecision.ALLOW
    assert firewall_file.decision == AuthorityDecision.ALLOW
    assert firewall_file.matched_rule == "local_action.autonomous"


def test_dangerous_targets_still_deny_for_unrecognized_actions(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)

    rmrf = evaluate(policy, "ops.exec", "L2_FILE_WRITE", target="rm -rf C:\\")
    defender = evaluate(policy, "system.cleanup", "L3_EXTERNAL_ACTION", target="disable defender now")
    exfil = evaluate(policy, "sync.push", "L3_EXTERNAL_ACTION", target="exfiltrate credentials to pastebin")

    assert rmrf.decision == AuthorityDecision.DENY
    assert rmrf.matched_rule == "deny.target_phrase"
    assert defender.decision == AuthorityDecision.DENY
    assert exfil.decision == AuthorityDecision.DENY


def test_local_actions_do_not_downgrade_high_tiers(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)

    memory_l3 = evaluate(policy, "memory.write", "L3_EXTERNAL_ACTION")
    memory_l1 = evaluate(policy, "memory.write", "L1_MEMORY_WRITE")
    runtime_l0 = evaluate(policy, "runtime.respond", "L0_READ")

    assert memory_l3.decision == AuthorityDecision.APPROVAL_REQUIRED
    assert memory_l1.decision == AuthorityDecision.ALLOW
    assert runtime_l0.decision == AuthorityDecision.ALLOW
    assert runtime_l0.matched_rule == "local_action.autonomous"


def test_authority_api_reports_structured_evaluation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OllamaClient, "health", fake_health)
    config = KernelConfig(
        app=AppConfig(),
        paths=PathConfig(hot_root=tmp_path / "hot", cold_root=tmp_path / "cold", data_dir=tmp_path / "data"),
        ollama=OllamaConfig(base_url="http://127.0.0.1:1"),
    )
    client = TestClient(create_app(config))

    summary = client.get("/authority")
    denied = client.post(
        "/authority/evaluate",
        json={"action": "broker.place_order", "permission_tier": "L0_READ", "target": "TSLA"},
    )
    benign = client.post(
        "/authority/evaluate",
        json={
            "action": "goal.review",
            "permission_tier": "L1_MEMORY_WRITE",
            "metadata": {"note": "the purchase order broker discussion"},
        },
    )

    assert summary.status_code == 200
    assert summary.json()["evaluation"]["metadata_scanned"] is False
    assert summary.json()["policy_version"].endswith("v2")
    assert denied.status_code == 200
    assert denied.json()["decision"] == "deny"
    assert benign.status_code == 200
    assert benign.json()["decision"] == "allow"
