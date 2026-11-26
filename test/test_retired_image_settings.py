import json
import tempfile
import unittest
from pathlib import Path

from services.config import ConfigStore


LEGACY_FIELDS = {
    "default_upstream_model_name": "legacy-model-extended",
    "default_thinking_effort": "max",
    "image_settle_enabled": True,
    "image_check_before_hit_enabled": True,
    "image_settle_secs": 8,
    "image_remove_conversation_after_result": True,
    "image_remove_conversation_always": True,
    "image_timeout_retry_secs": 60,
}


class RetiredImageSettingsTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "config.json"
        self.original = {
            "auth-key": "synthetic-admin-key",
            "image_poll_timeout_secs": 240,
            "base_url": "https://images.example.test",
            **LEGACY_FIELDS,
        }
        self.path.write_text(json.dumps(self.original), encoding="utf-8")
        self.store = ConfigStore(self.path)

    def test_load_omits_legacy_fields_without_rewriting_file(self):
        settings = self.store.get()
        self.assertTrue(LEGACY_FIELDS.keys().isdisjoint(settings))
        self.assertEqual(settings["image_poll_timeout_secs"], 240)
        self.assertEqual(settings["base_url"], "https://images.example.test")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), self.original)

    def test_save_discards_legacy_fields_and_persists_retained_settings(self):
        updates = {"image_poll_timeout_secs": 180, "base_url": ""}
        response = self.store.update({**LEGACY_FIELDS, **updates})
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        for settings in (response, saved):
            self.assertTrue(LEGACY_FIELDS.keys().isdisjoint(settings))
            self.assertEqual({key: settings[key] for key in updates}, updates)
        self.assertEqual(saved["auth-key"], "synthetic-admin-key")
