# Tora AI release packaging

The website is static and has no build step. From the repository root:

```powershell
python -m http.server 4173 --directory website/dist
```

To refresh the downloadable packages:

```powershell
Remove-Item -Recurse -Force release -ErrorAction SilentlyContinue
New-Item -ItemType Directory release | Out-Null
$source = @(
  "agent.py","bridge.py","config.py","gate.py","main.py","server.py","state.py",
  "tools_browser.py","tools_email.py","tools_fs.py","requirements.txt","requirements-gmail.txt",
  ".env.example","profile.example.yaml","README.md","QUICKSTART.md","GMAIL_SETUP.md",
  "CONTRIBUTING.md","PHASE2_BUILD_PLAN.md","scripts","edge_extension","mock_form",
  "edge_codrive.bat","start-website.bat","start-agent.bat",
  "test_browser.py","test_server.py","test_live_vlm.py","test_real_site.py","test_remote_operator.py",
  "test_state.py","test_tesla_live.py","test_tools_fs.py","test_vision.py"
)
Compress-Archive -Path $source -DestinationPath release/tora-ai-source.zip -Force
Compress-Archive -Path edge_extension -DestinationPath release/tora-edge-extension.zip -Force
Copy-Item QUICKSTART.md website/dist/downloads/QUICKSTART.md -Force
Copy-Item release/tora-ai-source.zip website/dist/downloads/tora-ai-source.zip -Force
Copy-Item release/tora-edge-extension.zip website/dist/downloads/tora-edge-extension.zip -Force
```
 Do not package `.env`, `.local-token`, `profile.yaml`, credentials, tokens, browser profiles, event logs, screenshots, artifacts, or virtual environments.
