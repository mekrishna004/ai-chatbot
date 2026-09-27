import json
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import bot


ROOT = Path(__file__).parent


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class VeraBotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.categories = {
            path.stem: load_json(path)
            for path in (ROOT / "dataset" / "categories").glob("*.json")
        }
        cls.merchants = {
            item["merchant_id"]: item
            for item in load_json(ROOT / "dataset" / "merchants_seed.json")["merchants"]
        }
        cls.customers = {
            item["customer_id"]: item
            for item in load_json(ROOT / "dataset" / "customers_seed.json")["customers"]
        }
        cls.triggers = {
            item["id"]: item
            for item in load_json(ROOT / "dataset" / "triggers_seed.json")["triggers"]
        }

    def setUp(self):
        self.client = TestClient(bot.app)
        self.client.post("/v1/teardown")

    def push(self, scope, context_id, payload, version=1):
        return self.client.post("/v1/context", json={
            "scope": scope,
            "context_id": context_id,
            "version": version,
            "payload": payload,
            "delivered_at": "2026-04-26T10:00:00Z",
        })

    def seed_contexts(self, merchant_ids, trigger_ids):
        for slug, category in self.categories.items():
            self.push("category", slug, category)
        for merchant_id in merchant_ids:
            self.push("merchant", merchant_id, self.merchants[merchant_id])
        for trigger_id in trigger_ids:
            self.push("trigger", trigger_id, self.triggers[trigger_id])

    def test_health_and_metadata_contract(self):
        health = self.client.get("/v1/healthz").json()
        self.assertEqual(health["status"], "ok")
        self.assertEqual(set(health["contexts_loaded"]), {"category", "merchant", "customer", "trigger"})
        metadata = self.client.get("/v1/metadata").json()
        self.assertEqual(metadata["model"], "deterministic-rules-v1")

    def test_context_idempotency_and_atomic_version_replacement(self):
        first = self.push("merchant", "m", {"value": 1}, version=2)
        duplicate = self.push("merchant", "m", {"value": 1}, version=2)
        stale = self.push("merchant", "m", {"value": 0}, version=1)
        update = self.push("merchant", "m", {"value": 2}, version=3)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(update.status_code, 200)
        self.assertEqual(bot._context("merchant", "m")["value"], 2)

    def test_context_rejects_unknown_scope_and_malformed_payload(self):
        bad_scope = self.push("planet", "x", {})
        bad_payload = self.client.post("/v1/context", content="{")
        self.assertEqual(bad_scope.status_code, 400)
        self.assertEqual(bad_scope.json()["reason"], "invalid_scope")
        self.assertEqual(bad_payload.status_code, 400)

    def test_digest_uses_cited_category_evidence_and_is_deterministic(self):
        category = self.categories["dentists"]
        merchant = self.merchants["m_001_drmeera_dentist_delhi"]
        trigger = self.triggers["trg_001_research_digest_dentists"]
        first = bot.compose(category, merchant, trigger)
        second = bot.compose(category, merchant, trigger)
        self.assertEqual(first, second)
        self.assertIn("2,100-participant", first["body"])
        self.assertIn("JIDA Oct 2026, p.14", first["body"])
        self.assertIn("high-risk adult", first["body"])
        self.assertEqual(first["send_as"], "vera")
        self.assertEqual(first["cta"], "open_ended")

    def test_competing_triggers_choose_one_higher_priority_action(self):
        merchant_id = "m_001_drmeera_dentist_delhi"
        trigger_ids = ["trg_001_research_digest_dentists", "trg_002_compliance_dci_radiograph"]
        self.seed_contexts([merchant_id], trigger_ids)
        request = {"now": "2026-04-26T10:30:00Z", "available_triggers": trigger_ids}
        result = self.client.post("/v1/tick", json=request).json()
        self.assertEqual(len(result["actions"]), 1)
        self.assertEqual(result["actions"][0]["trigger_id"], "trg_002_compliance_dci_radiograph")
        self.assertIn("Dental Council of India", result["actions"][0]["body"])

    def test_duplicate_tick_is_idempotent_and_new_tick_is_suppressed(self):
        merchant_id = "m_003_studio11_salon_hyderabad"
        trigger_id = "trg_008_curious_ask_studio11"
        self.seed_contexts([merchant_id], [trigger_id])
        request = {"now": "2026-04-26T10:30:00Z", "available_triggers": [trigger_id]}
        first = self.client.post("/v1/tick", json=request).json()
        duplicate = self.client.post("/v1/tick", json=request).json()
        later = self.client.post("/v1/tick", json={**request, "now": "2026-04-26T10:35:00Z"}).json()
        self.assertEqual(first, duplicate)
        self.assertEqual(len(first["actions"]), 1)
        self.assertEqual(later["actions"], [])

    def test_acceptance_handoff_returns_grounded_digest_summary(self):
        merchant_id = "m_001_drmeera_dentist_delhi"
        trigger_id = "trg_001_research_digest_dentists"
        self.seed_contexts([merchant_id], [trigger_id])
        tick = self.client.post("/v1/tick", json={
            "now": "2026-04-26T10:30:00Z", "available_triggers": [trigger_id],
        }).json()
        conversation_id = tick["actions"][0]["conversation_id"]
        response = self.client.post("/v1/reply", json={
            "conversation_id": conversation_id, "merchant_id": merchant_id,
            "from_role": "merchant", "message": "Yes, send me the abstract", "turn_number": 2,
        }).json()
        self.assertEqual(response["action"], "send")
        self.assertIn("38% lower caries recurrence", response["body"])
        self.assertIn("JIDA Oct 2026, p.14", response["body"])
        self.assertNotIn("move straight to the next step", response["body"])

    def test_expired_and_unknown_triggers_are_suppressed(self):
        merchant_id = "m_003_studio11_salon_hyderabad"
        trigger_id = "trg_008_curious_ask_studio11"
        self.seed_contexts([merchant_id], [trigger_id])
        expired = self.client.post("/v1/tick", json={"now": "2027-01-01T00:00:00Z", "available_triggers": [trigger_id, "unknown"]}).json()
        self.assertEqual(expired["actions"], [])

    def test_customer_recall_requires_matching_customer_and_consent(self):
        merchant_id = "m_001_drmeera_dentist_delhi"
        trigger_id = "trg_003_recall_due_priya"
        customer_id = "c_001_priya_for_m001"
        self.seed_contexts([merchant_id], [trigger_id])
        request = {"now": "2026-04-26T10:30:00Z", "available_triggers": [trigger_id]}
        self.assertEqual(self.client.post("/v1/tick", json=request).json()["actions"], [])
        self.push("customer", customer_id, self.customers[customer_id])
        action = self.client.post("/v1/tick", json=request).json()["actions"][0]
        self.assertEqual(action["send_as"], "merchant_on_behalf")
        self.assertIn("Hi Priya", action["body"])
        self.assertIn("Wed 5 Nov, 6pm", action["body"])
        self.assertIn("Apke liye", action["body"])
        self.assertNotIn("expired Deep Cleaning @ ₹499", action["body"])

    def test_customer_opt_out_and_wrong_merchant_are_suppressed(self):
        merchant_id = "m_001_drmeera_dentist_delhi"
        trigger_id = "trg_003_recall_due_priya"
        customer_id = "c_001_priya_for_m001"
        customer = json.loads(json.dumps(self.customers[customer_id]))
        customer["preferences"]["reminder_opt_in"] = False
        self.seed_contexts([merchant_id], [trigger_id])
        self.push("customer", customer_id, customer)
        request = {"now": "2026-04-26T10:30:00Z", "available_triggers": [trigger_id]}
        self.assertEqual(self.client.post("/v1/tick", json=request).json()["actions"], [])
        customer["preferences"]["reminder_opt_in"] = True
        customer["merchant_id"] = "different-merchant"
        self.push("customer", customer_id, customer, version=2)
        self.assertEqual(self.client.post("/v1/tick", json={**request, "now": "2026-04-26T10:35:00Z"}).json()["actions"], [])

    def test_sparse_trigger_uses_only_other_grounded_evidence(self):
        trigger = {"id": "new", "scope": "merchant", "kind": "perf_dip", "payload": {"placeholder": True}}
        result = bot.compose(self.categories["gyms"], self.merchants["m_007_powerhouse_gym_bangalore"], trigger)
        self.assertIn("views -30%", result["body"])
        unknown = {"id": "x", "scope": "merchant", "kind": "new_unknown_kind", "payload": {}}
        self.assertEqual(bot.compose(self.categories["gyms"], self.merchants["m_007_powerhouse_gym_bangalore"], unknown)["body"], "")

    def test_category_specific_pharmacy_alert_uses_batches_and_caution(self):
        result = bot.compose(
            self.categories["pharmacies"],
            self.merchants["m_009_apollo_pharmacy_jaipur"],
            self.triggers["trg_018_supply_atorvastatin_recall"],
        )
        self.assertIn("AT2024-1102", result["body"])
        self.assertIn("verify the distributor notice", result["body"])
        self.assertNotIn("dosage", result["body"].lower())

    def test_reply_state_machine_handles_acceptance_opt_out_question_and_topic(self):
        accepted = self.client.post("/v1/reply", json={
            "conversation_id": "accepted", "merchant_id": "m", "from_role": "merchant",
            "message": "Ok let's do it. What's next?", "turn_number": 2,
        }).json()
        stopped = self.client.post("/v1/reply", json={
            "conversation_id": "stopped", "merchant_id": "m", "from_role": "merchant",
            "message": "Stop messaging me. This is useless spam.", "turn_number": 2,
        }).json()
        question = self.client.post("/v1/reply", json={
            "conversation_id": "question", "from_role": "merchant", "message": "How much does that cost?", "turn_number": 2,
        }).json()
        off_topic = self.client.post("/v1/reply", json={
            "conversation_id": "topic", "from_role": "merchant", "message": "Can you help me file my GST?", "turn_number": 2,
        }).json()
        no_thanks = self.client.post("/v1/reply", json={
            "conversation_id": "no-thanks", "from_role": "merchant", "message": "No thanks", "turn_number": 2,
        }).json()
        self.assertEqual(accepted["action"], "send")
        self.assertIn("move straight", accepted["body"])
        self.assertEqual(stopped["action"], "end")
        self.assertEqual(question["action"], "send")
        self.assertIn("won't guess", question["body"])
        self.assertEqual(off_topic["action"], "send")
        self.assertIn("can't help with that request", off_topic["body"])
        self.assertEqual(no_thanks["action"], "end")

    def test_auto_reply_repetition_ends_after_three(self):
        results = []
        for turn in range(2, 5):
            results.append(self.client.post("/v1/reply", json={
                "conversation_id": "auto", "merchant_id": "m", "from_role": "merchant",
                "message": "Thank you for contacting us. Our team will respond shortly.", "turn_number": turn,
            }).json())
        self.assertEqual(results[0]["action"], "wait")
        self.assertEqual(results[1]["action"], "wait")
        self.assertEqual(results[2]["action"], "end")

    def test_auto_reply_streak_spans_fresh_conversation_ids(self):
        results = []
        for turn in range(1, 5):
            results.append(self.client.post("/v1/reply", json={
                "conversation_id": f"fresh-auto-{turn}", "merchant_id": "same-merchant",
                "from_role": "merchant",
                "message": "Thank you for contacting us. Our team will respond shortly.",
                "turn_number": turn + 1,
            }).json())
        self.assertEqual([item["action"] for item in results], ["wait", "wait", "end", "end"])

    def test_duplicate_reply_is_idempotent_and_customer_optout_is_scoped(self):
        request = {
            "conversation_id": "duplicate", "merchant_id": "merchant-x", "from_role": "merchant",
            "message": "Can you share details?", "turn_number": 2,
        }
        first = self.client.post("/v1/reply", json=request).json()
        duplicate = self.client.post("/v1/reply", json=request).json()
        self.assertEqual(first, duplicate)
        self.assertEqual(len(bot.CONVERSATIONS["duplicate"]["turns"]), 1)

        customer_request = {
            "conversation_id": "customer-stop", "merchant_id": "merchant-x", "customer_id": "customer-x",
            "from_role": "customer", "message": "STOP", "turn_number": 2,
        }
        response = self.client.post("/v1/reply", json=customer_request).json()
        self.assertEqual(response["action"], "end")
        self.assertIn("customer-x", bot.OPTED_OUT_CUSTOMERS)
        self.assertNotIn("merchant-x", bot.OPTED_OUT_MERCHANTS)

    def test_conversation_does_not_repeat_assistant_body(self):
        first = self.client.post("/v1/reply", json={
            "conversation_id": "no-repeat", "from_role": "merchant",
            "message": "How much does it cost?", "turn_number": 2,
        }).json()
        second = self.client.post("/v1/reply", json={
            "conversation_id": "no-repeat", "from_role": "merchant",
            "message": "How much does it cost?", "turn_number": 3,
        }).json()
        self.assertEqual(first["action"], "send")
        self.assertEqual(second["action"], "wait")

    def test_malformed_tick_and_reply_do_not_crash(self):
        self.assertEqual(self.client.post("/v1/tick", content="{").status_code, 400)
        malformed = self.client.post("/v1/reply", json={"conversation_id": "x"})
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(malformed.json()["action"], "end")


if __name__ == "__main__":
    unittest.main()