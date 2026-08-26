import json

PREMIUM = 10**18
PAYOUT = PREMIUM * 10

FLIGHT_URL = "https://flightdata.example.com/status/AC1902.json"
FLIGHT_REGEX = r"flightdata\.example\.com"


def _deploy(direct_vm, direct_deploy, owner):
    """Deploys with `owner` as the sender so the deployer becomes the contract owner."""
    direct_vm.sender = owner
    contract = direct_deploy("contracts/FlightDelayInsurer.py")
    contract.approve_source("https://flightdata.example.com/", "Example Flight Data")
    return contract


def _buy_policy(direct_vm, contract, buyer, policy_id, flight="AZ101", threshold=120):
    """Buys a policy with a 1 GEN premium (payout becomes 10 GEN), bound to the approved source."""
    direct_vm.sender = buyer
    direct_vm.value = PREMIUM
    contract.buy_policy(policy_id, flight, "2026-08-01", threshold, FLIGHT_URL)
    direct_vm.value = 0


def test_buy_policy_records_active_policy_with_ten_x_payout(
    direct_vm, direct_deploy, direct_alice
):
    """A bought policy is stored as active with payout equal to 10x the premium."""
    contract = _deploy(direct_vm, direct_deploy, direct_alice)
    _buy_policy(direct_vm, contract, direct_alice, "policy-1")

    policy = contract.get_policy("policy-1")
    assert len(policy["insured"]) > 0
    assert policy["flight"] == "AZ101"
    assert policy["date_iso"] == "2026-08-01"
    assert policy["threshold_minutes"] == 120
    assert policy["premium_atto"] == PREMIUM
    assert policy["payout_atto"] == PAYOUT
    assert policy["status"] == "active"
    assert policy["delay_minutes"] == 0
    assert policy["source_url"] == FLIGHT_URL
    assert contract.total_policies() == 1


def test_check_status_pays_when_delay_meets_threshold(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    """A 180-minute delay against a 120-minute threshold credits 10x premium."""
    contract = _deploy(direct_vm, direct_deploy, direct_alice)
    _buy_policy(direct_vm, contract, direct_alice, "policy-delayed")

    direct_vm.mock_web(
        FLIGHT_REGEX,
        {"status": 200, "body": json.dumps({"status": "DELAYED", "delay_minutes": 180})},
    )

    with direct_vm.prank(direct_bob):
        contract.check_status("policy-delayed")

    policy = contract.get_policy("policy-delayed")
    assert policy["status"] == "paid"
    assert policy["delay_minutes"] == 180
    assert contract.credit_of(direct_alice) == PAYOUT
    assert contract.credit_of(direct_bob) == 0


def test_check_status_denies_when_flight_on_time(
    direct_vm, direct_deploy, direct_alice
):
    """An on-time flight with a small delay denies the policy without payout."""
    contract = _deploy(direct_vm, direct_deploy, direct_alice)
    _buy_policy(direct_vm, contract, direct_alice, "policy-ontime")

    direct_vm.mock_web(
        FLIGHT_REGEX,
        {"status": 200, "body": json.dumps({"status": "ON TIME", "delay_minutes": 30})},
    )
    contract.check_status("policy-ontime")

    policy = contract.get_policy("policy-ontime")
    assert policy["status"] == "denied"
    assert policy["delay_minutes"] == 30
    assert contract.credit_of(direct_alice) == 0


def test_check_status_pays_on_cancelled_flight_with_zero_delay(
    direct_vm, direct_deploy, direct_alice
):
    """A CANCELLED status triggers payout even when reported delay is zero."""
    contract = _deploy(direct_vm, direct_deploy, direct_alice)
    _buy_policy(direct_vm, contract, direct_alice, "policy-cancelled")

    direct_vm.mock_web(
        FLIGHT_REGEX,
        {"status": 200, "body": json.dumps({"status": "CANCELLED", "delay_minutes": 0})},
    )
    contract.check_status("policy-cancelled")

    policy = contract.get_policy("policy-cancelled")
    assert policy["status"] == "paid"
    assert policy["delay_minutes"] == 0
    assert contract.credit_of(direct_alice) == PAYOUT


def test_check_status_pays_at_exact_threshold_boundary(
    direct_vm, direct_deploy, direct_alice
):
    """A delay exactly equal to the threshold counts as triggered."""
    contract = _deploy(direct_vm, direct_deploy, direct_alice)
    _buy_policy(direct_vm, contract, direct_alice, "policy-boundary")

    direct_vm.mock_web(
        FLIGHT_REGEX,
        {"status": 200, "body": json.dumps({"status": "DELAYED", "delay_minutes": 120})},
    )
    contract.check_status("policy-boundary")

    policy = contract.get_policy("policy-boundary")
    assert policy["status"] == "paid"
    assert policy["delay_minutes"] == 120
    assert contract.credit_of(direct_alice) == PAYOUT


def test_buy_policy_rejects_zero_value_or_zero_threshold(
    direct_vm, direct_deploy, direct_alice
):
    """Buying requires positive attached value and a positive threshold."""
    contract = _deploy(direct_vm, direct_deploy, direct_alice)

    direct_vm.sender = direct_alice
    direct_vm.value = 0
    with direct_vm.expect_revert("Send value with the call"):
        contract.buy_policy("policy-zero", "AZ101", "2026-08-01", 120, FLIGHT_URL)

    direct_vm.value = PREMIUM
    with direct_vm.expect_revert("Threshold must be greater than zero"):
        contract.buy_policy("policy-nothresh", "AZ101", "2026-08-01", 0, FLIGHT_URL)
    direct_vm.value = 0

    assert contract.total_policies() == 0


def test_buy_policy_rejects_duplicate_id(direct_vm, direct_deploy, direct_alice):
    """Policy ids are unique: buying twice with the same id reverts."""
    contract = _deploy(direct_vm, direct_deploy, direct_alice)
    _buy_policy(direct_vm, contract, direct_alice, "policy-dup")

    direct_vm.sender = direct_alice
    direct_vm.value = PREMIUM
    with direct_vm.expect_revert("Policy id already exists"):
        contract.buy_policy("policy-dup", "AZ102", "2026-08-02", 60, FLIGHT_URL)
    direct_vm.value = 0

    assert contract.total_policies() == 1


def test_check_status_rejects_unknown_or_non_active_policy(
    direct_vm, direct_deploy, direct_alice
):
    """Checking an unknown id or an already-resolved policy reverts."""
    contract = _deploy(direct_vm, direct_deploy, direct_alice)
    _buy_policy(direct_vm, contract, direct_alice, "policy-live")

    with direct_vm.expect_revert("Unknown policy id"):
        contract.check_status("missing-policy")

    direct_vm.mock_web(
        FLIGHT_REGEX,
        {"status": 200, "body": json.dumps({"status": "DELAYED", "delay_minutes": 180})},
    )
    contract.check_status("policy-live")
    assert contract.get_policy("policy-live")["status"] == "paid"

    with direct_vm.expect_revert("Policy is not active"):
        contract.check_status("policy-live")


def test_check_status_maps_http_500_to_transient(
    direct_vm, direct_deploy, direct_alice
):
    """A server error surfaces as [TRANSIENT] and leaves the policy active."""
    contract = _deploy(direct_vm, direct_deploy, direct_alice)
    _buy_policy(direct_vm, contract, direct_alice, "policy-500")

    direct_vm.mock_web(FLIGHT_REGEX, {"status": 500, "body": "boom"})
    with direct_vm.expect_revert("[TRANSIENT]"):
        contract.check_status("policy-500")

    policy = contract.get_policy("policy-500")
    assert policy["status"] == "active"
    assert policy["delay_minutes"] == 0
    assert contract.credit_of(direct_alice) == 0


def test_unapproved_flight_source_rejected(direct_vm, direct_deploy, direct_alice):
    """Policies cannot bind a flight data source outside owner-approved prefixes."""
    contract = _deploy(direct_vm, direct_deploy, direct_alice)
    direct_vm.sender = direct_alice
    direct_vm.value = 10**18
    with direct_vm.expect_revert("not owner-approved"):
        contract.buy_policy("p-evil", "AC1902", "2026-09-01", 120, "https://evil.example.com/fake.json")
    direct_vm.value = 0
    assert contract.total_policies() == 0


def test_source_registry_owner_only_and_views(direct_vm, direct_deploy, direct_alice, direct_bob):
    """Only the owner manages sources; views expose the registry."""
    contract = _deploy(direct_vm, direct_deploy, direct_alice)
    with direct_vm.prank(direct_bob):
        with direct_vm.expect_revert("Only owner may approve"):
            contract.approve_source("https://x.example.com/", "X")
    direct_vm.sender = direct_alice
    contract.approve_source("https://extra.example.com/", "Extra")
    assert contract.is_source_approved("https://extra.example.com/a.json") is True
    assert contract.is_source_approved("https://unknown.example.com/a.json") is False
    contract.revoke_source("https://extra.example.com/")
    assert contract.is_source_approved("https://extra.example.com/a.json") is False
    with direct_vm.expect_revert("not approved"):
        contract.revoke_source("https://extra.example.com/")
