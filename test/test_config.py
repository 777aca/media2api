import json
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
ROOT_CONFIG_FILE = ROOT_DIR / "config.json"


class ConfigLoadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._created_root_config = False
        if not ROOT_CONFIG_FILE.exists():
            ROOT_CONFIG_FILE.write_text(json.dumps({"auth-key": "test-auth"}), encoding="utf-8")
            cls._created_root_config = True

        from services import config as config_module

        cls.config_module = config_module

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._created_root_config and ROOT_CONFIG_FILE.exists():
            ROOT_CONFIG_FILE.unlink()

    def test_load_settings_ignores_directory_config_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            data_dir = base_dir / "data"
            config_dir = base_dir / "config.json"
            os_auth_key = "env-auth"

            config_dir.mkdir()

            module = self.config_module
            old_base_dir = module.BASE_DIR
            old_data_dir = module.DATA_DIR
            old_config_file = module.CONFIG_FILE
            old_env_auth_key = module.os.environ.get("MEDIA2API_AUTH_KEY")
            try:
                module.BASE_DIR = base_dir
                module.DATA_DIR = data_dir
                module.CONFIG_FILE = config_dir
                module.os.environ["MEDIA2API_AUTH_KEY"] = os_auth_key

                settings = module._load_settings()

                self.assertEqual(settings.auth_key, os_auth_key)
                self.assertEqual(settings.refresh_account_interval_minute, 5)
            finally:
                module.BASE_DIR = old_base_dir
                module.DATA_DIR = old_data_dir
                module.CONFIG_FILE = old_config_file
                if old_env_auth_key is None:
                    module.os.environ.pop("MEDIA2API_AUTH_KEY", None)
                else:
                    module.os.environ["MEDIA2API_AUTH_KEY"] = old_env_auth_key

    def test_removed_canvas_settings_preserve_legacy_file_and_other_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config.json"
            legacy_apps = {"infinite_canvas": {"enabled": True, "url": "https://canvas.example.test"}}
            original = {
                "auth-key": "synthetic-auth-key",
                "base_url": "https://api.example.test",
                "third_party_apps": legacy_apps,
                "image_account_concurrency": 3,
            }
            config_file.write_text(json.dumps(original), encoding="utf-8")
            store = self.config_module.ConfigStore(config_file)

            self.assertNotIn("third_party_apps", store.get())
            self.assertEqual(json.loads(config_file.read_text(encoding="utf-8")), original)

            updated = store.update({
                "base_url": "https://updated.example.test",
                "third_party_apps": {"infinite_canvas": {"enabled": False, "url": "https://other.example.test"}},
            })
            self.assertNotIn("third_party_apps", updated)
            self.assertEqual(updated["base_url"], "https://updated.example.test")
            self.assertEqual(updated["image_account_concurrency"], 3)
            persisted = json.loads(config_file.read_text(encoding="utf-8"))
            self.assertEqual(persisted["third_party_apps"], legacy_apps)
            self.assertEqual(persisted["auth-key"], original["auth-key"])

    def test_legacy_client_cannot_add_removed_canvas_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config.json"
            config_file.write_text(json.dumps({"auth-key": "synthetic-auth-key"}), encoding="utf-8")
            store = self.config_module.ConfigStore(config_file)

            updated = store.update({
                "third_party_apps": {"infinite_canvas": {"enabled": True, "url": "https://canvas.example.test"}},
                "image_account_concurrency": 5,
            })
            self.assertNotIn("third_party_apps", updated)
            self.assertNotIn("third_party_apps", json.loads(config_file.read_text(encoding="utf-8")))
            self.assertEqual(updated["image_account_concurrency"], 5)


if __name__ == "__main__":
    unittest.main()
