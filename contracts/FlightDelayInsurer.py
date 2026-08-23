# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

from genlayer import *
from dataclasses import dataclass
import json


ERROR_EXPECTED = "[EXPECTED]"
ERROR_EXTERNAL = "[EXTERNAL]"
ERROR_TRANSIENT = "[TRANSIENT]"

STATUS_ACTIVE = "active"
STATUS_PAID = "paid"
STATUS_DENIED = "denied"

PAYOUT_MULTIPLIER = 10
TRIGGER_STATUSES = ("CANCELLED", "DIVERTED")
DELAY_TOLERANCE_MINUTES = 10


def _handle_leader_error(leaders_res, leader_fn) -> bool:
	leader_msg = leaders_res.message if hasattr(leaders_res, "message") else ""
	try:
		leader_fn()
		return False
	except gl.vm.UserError as e:
		validator_msg = e.message if hasattr(e, "message") else str(e)
		if validator_msg.startswith(ERROR_EXPECTED) or validator_msg.startswith(ERROR_EXTERNAL):
			return validator_msg == leader_msg
		if validator_msg.startswith(ERROR_TRANSIENT) and leader_msg.startswith(ERROR_TRANSIENT):
			return True
		return False
	except Exception:
		return False


@gl.evm.contract_interface
class _Recipient:
	class View:
		pass

	class Write:
		pass


@allow_storage
@dataclass
class Policy:
	insured: Address
	flight: str
	date_iso: str
	threshold_minutes: u256
	premium_atto: u256
	payout_atto: u256
	status: str
	delay_minutes: u256


class FlightDelayInsurer(gl.Contract):
	owner_addr: Address
	policies: TreeMap[str, Policy]
	credits: TreeMap[Address, u256]
	policy_ids: DynArray[str]

	def __init__(self) -> None:
		self.owner_addr = gl.message.sender_address

	def _get_policy(self, policy_id: str) -> Policy:
		policy = self.policies.get(str(policy_id))
		if policy is None:
			raise gl.vm.UserError(f"{ERROR_EXPECTED} Unknown policy id")
		return policy

	@gl.public.view
	def owner(self) -> Address:
		return self.owner_addr

	@gl.public.write.payable
	def buy_policy(
		self,
		policy_id: str,
		flight: str,
		date_iso: str,
		threshold_minutes: u256,
	) -> None:
		if gl.message.value == u256(0):
			raise gl.vm.UserError(f"{ERROR_EXPECTED} Send value with the call")
		if u256(threshold_minutes) == u256(0):
			raise gl.vm.UserError(f"{ERROR_EXPECTED} Threshold must be greater than zero")
		if str(policy_id) in self.policies:
			raise gl.vm.UserError(f"{ERROR_EXPECTED} Policy id already exists")
		premium_atto = u256(gl.message.value)
		payout_atto = u256(int(premium_atto) * PAYOUT_MULTIPLIER)
		self.policies[str(policy_id)] = Policy(
			insured=gl.message.sender_address,
			flight=str(flight),
			date_iso=str(date_iso),
			threshold_minutes=u256(threshold_minutes),
			premium_atto=premium_atto,
			payout_atto=payout_atto,
			status=STATUS_ACTIVE,
			delay_minutes=u256(0),
		)
		self.policy_ids.append(str(policy_id))

	@gl.public.write
	def check_status(self, policy_id: str, source_url: str) -> None:
		policy = self._get_policy(policy_id)
		if policy.status != STATUS_ACTIVE:
			raise gl.vm.UserError(f"{ERROR_EXPECTED} Policy is not active")
		url = str(source_url)
		threshold = int(policy.threshold_minutes)

		def leader_fn() -> dict:
			res = gl.nondet.web.get(url)
			if res.status >= 500:
				raise gl.vm.UserError(
					f"{ERROR_TRANSIENT} Flight API returned status {res.status}"
				)
			if res.status >= 400:
				raise gl.vm.UserError(f"{ERROR_EXTERNAL} Flight API returned status {res.status}")
			try:
				payload = json.loads(bytes(res.body).decode("utf-8"))
			except Exception:
				raise gl.vm.UserError(f"{ERROR_EXTERNAL} malformed flight payload")
			if not isinstance(payload, dict):
				raise gl.vm.UserError(f"{ERROR_EXTERNAL} unexpected flight payload shape")
			status_l = str(payload.get("status", "")).upper()
			raw_delay = payload.get("delay_minutes", 0)
			if raw_delay is None:
				raw_delay = 0
			try:
				delayed_min = int(raw_delay)
			except Exception:
				delayed_min = 0
			triggered = status_l in TRIGGER_STATUSES or delayed_min >= threshold
			return {"triggered": bool(triggered), "delayed_min": int(delayed_min)}

		def validator_fn(leaders_res: gl.vm.Result) -> bool:
			if not isinstance(leaders_res, gl.vm.Return):
				return _handle_leader_error(leaders_res, leader_fn)
			leader_data = leaders_res.calldata
			if not isinstance(leader_data, dict):
				return False
			fresh = leader_fn()
			leader_triggered = bool(leader_data.get("triggered", False))
			fresh_triggered = bool(fresh.get("triggered", False))
			if leader_triggered != fresh_triggered:
				return False
			leader_delay = int(leader_data.get("delayed_min", 0))
			fresh_delay = int(fresh.get("delayed_min", 0))
			return abs(leader_delay - fresh_delay) <= DELAY_TOLERANCE_MINUTES

		result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

		policy.delay_minutes = u256(int(result["delayed_min"]))
		if bool(result["triggered"]):
			policy.status = STATUS_PAID
			insured = policy.insured
			self.credits[insured] = self.credits.get(insured, u256(0)) + u256(
				policy.payout_atto
			)
		else:
			policy.status = STATUS_DENIED

	@gl.public.write
	def withdraw(self) -> None:
		who = gl.message.sender_address
		amount = self.credits.get(who, u256(0))
		if amount == u256(0):
			raise gl.vm.UserError(f"{ERROR_EXPECTED} Nothing to withdraw")
		self.credits[who] = u256(0)
		_Recipient(Address(str(who))).emit_transfer(value=u256(amount))

	@gl.public.view
	def get_policy(self, policy_id: str) -> dict:
		policy = self._get_policy(policy_id)
		return {
			"insured": str(policy.insured),
			"flight": policy.flight,
			"date_iso": policy.date_iso,
			"threshold_minutes": policy.threshold_minutes,
			"premium_atto": policy.premium_atto,
			"payout_atto": policy.payout_atto,
			"status": policy.status,
			"delay_minutes": policy.delay_minutes,
		}

	@gl.public.view
	def credit_of(self, who: Address) -> u256:
		key = who if isinstance(who, Address) else Address(who)
		return self.credits.get(key, u256(0))

	@gl.public.view
	def total_policies(self) -> u256:
		return u256(len(self.policy_ids))
