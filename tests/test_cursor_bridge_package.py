import json
import pathlib
import subprocess
import sys
import unittest
import zipfile
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[1]


class CursorBridgePackageTests(unittest.TestCase):
    def test_vsix_manifest_matches_payload(self):
        bridge = ROOT / 'integrations/cursor-bridge'
        subprocess.run([sys.executable, str(bridge / 'package.py')], check=True, capture_output=True)
        with zipfile.ZipFile(bridge / 'agent-panel-bridge.vsix') as archive:
            package = json.loads(archive.read('extension/package.json'))
            manifest = ET.fromstring(archive.read('extension.vsixmanifest'))
            identity = manifest.find('{*}Metadata/{*}Identity')
            self.assertEqual(identity.attrib['Version'], package['version'])
            self.assertEqual(identity.attrib['Publisher'], package['publisher'])
            self.assertEqual(identity.attrib['Id'], package['name'])
            self.assertEqual(archive.read('extension/extension.js'), (bridge / 'extension.js').read_bytes())

    def test_bridge_is_bundled_before_signing(self):
        script = (ROOT / 'scripts/build.sh').read_text()
        self.assertLess(script.index('cp integrations/cursor-bridge/agent-panel-bridge.vsix'), script.index('codesign --force'))
        for name in ('ci.yml', 'release.yml'):
            text = (ROOT / '.github/workflows' / name).read_text()
            self.assertIn('test -f AgentTaskPanel.app/Contents/Resources/agent-panel-bridge.vsix', text)
