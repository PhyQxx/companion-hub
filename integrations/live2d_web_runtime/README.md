# Aria Live2D Web runtime adapter

This adapter wraps the official Cubism SDK for Web R5 Framework/Sample code in
the `window.AriaLive2DRuntime.mount()` interface used by Companion Hub. The
official proprietary Cubism Core is intentionally not stored in this repo.

Build it with an SDK downloaded after accepting Live2D's license:

```bash
ln -s "/path/to/CubismSdkForWeb-5-r.5" sdk
ARIA_CUBISM_SDK_ROOT="/path/to/CubismSdkForWeb-5-r.5" \
  ../../web/apps/chat/node_modules/.bin/vite build --config vite.config.mjs
```

Install `dist/adapter.js`, `runtime.js`, the licensed Core file, and the
Framework `Shaders` directory together. On macOS the default runtime location
is `~/Library/Application Support/AriaCompanionHub/live2d-runtime/current`.
