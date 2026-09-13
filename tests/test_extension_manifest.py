import base64
import hashlib
import json
import os
import subprocess
import unittest
import zipfile
from scripts.install_native_host import derive_chrome_extension_id, get_default_chrome_extension_ids


class TestExtensionManifests(unittest.TestCase):
    def setUp(self):
        self.repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.extension_dir = os.path.join(self.repo_root, "extension")

    def test_chrome_manifest_mv3_compliance(self):
        manifest_path = os.path.join(self.extension_dir, "manifest.json")
        self.assertTrue(os.path.exists(manifest_path), "manifest.json must exist")

        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data.get("manifest_version"), 3, "Chrome manifest must be MV3")
        self.assertIn("background", data)
        self.assertIn("service_worker", data["background"])
        self.assertNotIn("scripts", data["background"], "MV3 background must not declare 'scripts'")
        self.assertNotIn("browser_specific_settings", data, "Gecko settings should not be in Chrome manifest")

        # Verify background service worker file exists
        sw_file = os.path.join(self.extension_dir, data["background"]["service_worker"])
        self.assertTrue(os.path.exists(sw_file), f"Service worker file missing: {sw_file}")

        # Verify tabs and core permissions
        permissions = data.get("permissions", [])
        self.assertIn("tabs", permissions, "MV3 manifest must include 'tabs' for tab.url/title access")
        self.assertIn("downloads", permissions)
        self.assertIn("nativeMessaging", permissions)
        self.assertIn("storage", permissions)
        self.assertIn("cookies", permissions)
        self.assertIn("contextMenus", permissions)
        self.assertIn("webRequest", permissions)

        # Verify key exists and generates valid 32-character extension ID
        key = data.get("key")
        self.assertIsNotNone(key, "Chrome manifest must declare fixed 'key' for native messaging origin parity")
        ext_id = derive_chrome_extension_id(key)
        self.assertEqual(len(ext_id), 32, f"Derived extension ID must be 32 chars, got {ext_id}")
        self.assertTrue(all(c in "abcdefghijklmnop" for c in ext_id), "Extension ID must use a-p alphabet")

        # Verify content scripts and main-world hook
        content_scripts = data.get("content_scripts", [])
        self.assertGreater(len(content_scripts), 0)
        has_main_world = any(cs.get("world") == "MAIN" for cs in content_scripts)
        self.assertTrue(has_main_world, "Chrome manifest should have MAIN world content script for page_hook.js")

        for cs in content_scripts:
            for js_path in cs.get("js", []):
                self.assertTrue(os.path.exists(os.path.join(self.extension_dir, js_path)))
            for css_path in cs.get("css", []):
                self.assertTrue(os.path.exists(os.path.join(self.extension_dir, css_path)))

        # Verify web_accessible_resources format (array of objects in MV3)
        war = data.get("web_accessible_resources", [])
        self.assertIsInstance(war, list)
        if war:
            self.assertIsInstance(war[0], dict, "MV3 web_accessible_resources must be list of dicts with resources and matches")
            self.assertIn("resources", war[0])
            self.assertIn("matches", war[0])

        # Verify action popup and icons
        if "action" in data:
            popup_html = data["action"].get("default_popup")
            if popup_html:
                self.assertTrue(os.path.exists(os.path.join(self.extension_dir, popup_html)))

        # Verify all icons exist
        if "icons" in data:
            for size, icon_rel_path in data["icons"].items():
                self.assertTrue(
                    os.path.exists(os.path.join(self.extension_dir, icon_rel_path)),
                    f"Icon {size} missing at {icon_rel_path}",
                )

    def test_firefox_manifest_compliance(self):
        manifest_path = os.path.join(self.extension_dir, "manifest.firefox.json")
        self.assertTrue(os.path.exists(manifest_path), "manifest.firefox.json must exist")

        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data.get("manifest_version"), 2)
        self.assertIn("background", data)
        self.assertIn("scripts", data["background"])
        self.assertIn("browser_specific_settings", data)
        self.assertIn("gecko", data["browser_specific_settings"])
        gecko = data["browser_specific_settings"]["gecko"]
        self.assertEqual(gecko.get("id"), "pv-idm@pv-idm.local")
        self.assertIn("data_collection_permissions", gecko)
        self.assertEqual(gecko["data_collection_permissions"].get("required"), ["none"])

        # Verify tabs permission for parity
        permissions = data.get("permissions", [])
        self.assertIn("tabs", permissions)

    def test_native_host_allowed_origins_compliance(self):
        chrome_ids = get_default_chrome_extension_ids(self.repo_root)
        self.assertGreater(len(chrome_ids), 0)
        for cid in chrome_ids:
            self.assertEqual(len(cid), 32)
            self.assertTrue(all(c in "abcdefghijklmnop" for c in cid))

    def test_extension_packaging_script(self):
        import sys
        pkg_script = os.path.join(self.repo_root, "scripts", "package_extensions.py")
        res = subprocess.run([sys.executable, pkg_script], cwd=self.repo_root, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Packaging failed: {res.stderr}")

        pv_chrome_zip = os.path.join(self.repo_root, "dist", "pv-idm-extension-chrome-mv3.zip")
        pv_firefox_zip = os.path.join(self.repo_root, "dist", "pv-idm-extension-firefox.zip")
        self.assertTrue(os.path.exists(pv_chrome_zip), "PV-IDM Chrome zip must be built")
        self.assertTrue(os.path.exists(pv_firefox_zip), "PV-IDM Firefox zip must be built")

        chrome_zip = os.path.join(self.repo_root, "dist", "idm-linux-extension-chrome-mv3.zip")
        firefox_zip = os.path.join(self.repo_root, "dist", "idm-linux-extension-firefox.zip")
        self.assertTrue(os.path.exists(chrome_zip), "Chrome zip must be built")
        self.assertTrue(os.path.exists(firefox_zip), "Firefox zip must be built")

        with zipfile.ZipFile(chrome_zip, "r") as zf:
            names = zf.namelist()
            self.assertIn("manifest.json", names)
            self.assertIn("background/service_worker.js", names)
            self.assertIn("content/video_sniffer.js", names)
            self.assertIn("content/page_hook.js", names)
            self.assertIn("popup/popup.html", names)
            manifest_content = json.loads(zf.read("manifest.json").decode("utf-8"))
            self.assertEqual(manifest_content.get("manifest_version"), 3)

        with zipfile.ZipFile(firefox_zip, "r") as zf:
            names = zf.namelist()
            self.assertIn("manifest.json", names)
            self.assertIn("background/service_worker.js", names)
            manifest_content = json.loads(zf.read("manifest.json").decode("utf-8"))
            self.assertEqual(manifest_content.get("manifest_version"), 2)
            self.assertIn("browser_specific_settings", manifest_content)
            gecko = manifest_content["browser_specific_settings"]["gecko"]
            self.assertIn("data_collection_permissions", gecko)
            self.assertEqual(gecko["data_collection_permissions"].get("required"), ["none"])

    def test_firefox_addons_linter_compliance(self):
        """Validate Firefox extension zip against official addons-linter if npx is available."""
        import shutil
        npx_bin = shutil.which("npx")
        if not npx_bin:
            self.skipTest("npx binary not available for addons-linter validation")

        firefox_zip = os.path.join(self.repo_root, "dist", "pv-idm-extension-firefox.zip")
        if not os.path.exists(firefox_zip):
            from scripts.package_extensions import package_extensions
            package_extensions()

        res = subprocess.run(
            [npx_bin, "--yes", "addons-linter", "--output=json", firefox_zip],
            capture_output=True,
            text=True,
            cwd=self.repo_root,
        )
        self.assertEqual(res.returncode, 0, f"addons-linter command failed: {res.stderr}")
        try:
            lint_result = json.loads(res.stdout)
            errors = lint_result.get("errors", [])
            warnings = lint_result.get("warnings", [])
            self.assertEqual(len(errors), 0, f"addons-linter reported errors: {errors}")
            self.assertEqual(len(warnings), 0, f"addons-linter reported warnings: {warnings}")
        except json.JSONDecodeError:
            self.fail(f"Failed to parse addons-linter output as JSON: {res.stdout}")

    def test_extension_javascript_syntax(self):
        """Validate JavaScript syntax across all extension source files via node --check if available."""
        import shutil
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node binary not available for JS syntax checking")

        js_files = []
        for root, _, files in os.walk(self.extension_dir):
            for file in files:
                if file.endswith(".js"):
                    js_files.append(os.path.join(root, file))

        self.assertGreater(len(js_files), 0, "Extension must contain JavaScript files")
        for js_file in js_files:
            res = subprocess.run([node_bin, "--check", js_file], capture_output=True, text=True)
            self.assertEqual(
                res.returncode, 0,
                f"JavaScript syntax error in {os.path.relpath(js_file, self.repo_root)}:\n{res.stderr}"
            )


if __name__ == "__main__":
    unittest.main()
