(function () {
  "use strict";
  var script = document.currentScript;
  var base = new URL("./", script && script.src ? script.src : window.location.href);

  function load(name, onload) {
    var element = document.createElement("script");
    element.src = new URL(name, base).href;
    element.async = true;
    element.onload = onload;
    element.onerror = function () {
      console.error("[Aria Live2D] Failed to load " + name);
      window.dispatchEvent(new Event("aria-live2d-runtime-error"));
    };
    document.head.appendChild(element);
  }

  if (window.Live2DCubismCore) load("adapter.js");
  else load("live2dcubismcore.min.js", function () { load("adapter.js"); });
})();
