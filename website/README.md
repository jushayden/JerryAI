# Tora AI website

This is a buildless static presentation site. Serve `dist/` locally with any static server, or deploy the contents of `dist/` to a static host.

```powershell
python -m http.server 4173 --directory website/dist
```

The site is intentionally separate from the local agent. `site-config.js` contains only public presentation settings. Set `demoVideoUrl` to a recorded HTTPS video or a same-origin asset when one is ready. The `live-demo-slot` is intentionally not wired to `127.0.0.1`; exposing a visitor's local agent through a marketing page would be unsafe.

Download links under `dist/downloads/` are generated release assets. Rebuild them after source changes with the commands in the root `RELEASE.md`.
