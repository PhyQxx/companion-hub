# Live2D 运行时接入

> 文档类型：Active 专项设计
> 最后更新：2026-08-27
> 当前状态：Web 最小壳与 Tauri 透明桌宠代码已落地，包含签名情绪/动作/口型联动、设备身份下的迷你输入与可选 TTS 播报、多屏恢复和隐藏资源释放；M2 延迟与桌宠真机性能仍是发布门槛。

形象中心可以安全导入 Live2D ZIP 模型包，也支持同时包含 FREE/PRO、`.cmo3`、`.can3` 和说明文件的官方发行包。检测到多个 `.model3.json` 时优先选择 PRO runtime；只提取选中 runtime 下的白名单资源，工程源文件不会落盘。模型包不允许符号链接、路径穿越或外部网络引用。

Live2D Cubism Core 受官方许可约束，本仓库不直接分发。macOS 本机安装器默认把已获许可的运行时放到 `~/Library/Application Support/AriaCompanionHub/live2d-runtime/current`；也可以通过 `ARIA_LIVE2D_RUNTIME_DIR` 指向其他目录。服务启动后，聊天端和 Admin 预览会自动从 `/api/v1/avatar-live2d-runtime/runtime.js` 加载它，无需重新构建前端。

`runtime.js` 必须注册以下全局适配器：

```js
window.AriaLive2DRuntime = {
  async mount(canvas, { modelUrl, transparent }) {
    // 使用已获许可的 Cubism Web Core + Framework 加载 modelUrl 并绘制到 canvas。
    // 禁止模型包发起外部网络请求。
    return {
      setLipSync(value) {
        // 接收 0..1 的 TTS viseme / 音量值，驱动模型 LipSync 参数。
      },
      setSpeaking(active) {
        // 进入或退出说话状态，可触发模型自带的说话动作。
      },
      setEmotion(emotion) {
        // 将 neutral / happy / sad / angry / surprised / thinking / concerned
        // 映射到模型已有的表情和动作；不存在时安全忽略。
      },
      setExpression(name) {
        // 播放回复控制块显式指定的表情。
      },
      playMotion(group, index) {
        // 播放模型包已有的动作组，不执行模型包中的脚本。
      },
      destroy() {
        // 释放 WebGL、纹理、监听器和模型资源。
      },
    };
  },
};
```

仓库中的 `integrations/live2d_web_runtime` 是基于官方 R5 Sample 改造的适配层。构建时需设置 `ARIA_CUBISM_SDK_ROOT`，其产物不会把 Core 提交到仓库。适配器负责画布缩放、模型加载完成检查、资源销毁和模型 URL 同源限制。聊天端会把 TTS viseme、说话状态、回复情绪、显式表情和 `animation` 动作指令转发给这些控制方法；模型没有对应能力时安全忽略。未配置运行时时，界面会显示“模型包校验通过，需安装官方 Cubism Web Core”，不会用 CSS 或假动画冒充 Live2D 渲染。
