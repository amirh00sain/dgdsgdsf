# SpiderPanel Vercel WebSocket relay

The current Vercel relay implementation is a WebSocket bridge implemented for SpiderPanel. It uses Vercel Functions' Node.js WebSocket support and the `ws` package to bridge `wss://<vercel-domain>/ws/vl/<uuid>` to the same path on the public Railway panel domain.

The repository previously included an HTTP/XHTTP relay adapted from XHTTP-Installer by avaco_cloud. That older transport is no longer used by the Vercel Domain feature. The accompanying third-party license file is retained as provenance for the earlier implementation; the present `deploy/vercel/api/index.js` is a WebSocket bridge.
