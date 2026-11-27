"""Build a dependency-free VSIX from the local bridge sources."""
from pathlib import Path
import zipfile
import json
from xml.sax.saxutils import escape
root = Path(__file__).resolve().parent
out = root / 'agent-panel-bridge.vsix'
metadata = json.loads((root / 'package.json').read_text())
manifest = '''<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
<Metadata><Identity Language="en-US" Id="agent-panel-bridge" Version="__VERSION__" Publisher="agent-task-panel"/><DisplayName>Agent Task Panel Bridge</DisplayName><Description xml:space="preserve">Open Cursor conversations in their IDE workspace.</Description><Tags>cursor</Tags><Categories>Other</Categories><GalleryFlags>Public</GalleryFlags><Properties><Property Id="Microsoft.VisualStudio.Code.Engine" Value="^1.85.0"/><Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value="ui"/></Properties></Metadata>
<Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation><Dependencies/>
<Assets><Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/></Assets></PackageManifest>'''
manifest = manifest.replace('__VERSION__', escape(metadata['version']))
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
    z.writestr('extension.vsixmanifest', manifest)
    z.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="json" ContentType="application/json"/><Default Extension="js" ContentType="application/javascript"/><Default Extension="vsixmanifest" ContentType="text/xml"/></Types>')
    for name in ('package.json', 'extension.js'):
        z.write(root / name, 'extension/' + name)
print(out)
