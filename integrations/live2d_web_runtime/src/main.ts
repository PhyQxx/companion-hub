/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */

import { LAppDelegate } from './lappdelegate';

interface RuntimeHandle {
  destroy(): void;
  setLipSync(value: number): void;
  setSpeaking(active: boolean): void;
  setExpression(expression: string): boolean;
  setEmotion(emotion: string): void;
  playMotion(group: string, index?: number): boolean;
}

interface RuntimeOptions {
  modelUrl: string;
  transparent: boolean;
}

let currentHandle: RuntimeHandle | null = null;

window.AriaLive2DRuntime = {
  async mount(
    canvas: HTMLCanvasElement,
    options: RuntimeOptions
  ): Promise<RuntimeHandle> {
    const modelUrl = new URL(options.modelUrl, window.location.href);
    if (
      modelUrl.origin !== window.location.origin ||
      !modelUrl.pathname.startsWith('/api/v1/avatar-user-assets/')
    ) {
      throw new Error('Live2D model URL must use the local avatar asset endpoint');
    }

    currentHandle?.destroy();
    let active = true;
    const handle: RuntimeHandle = {
      destroy(): void {
        if (!active) return;
        active = false;
        if (currentHandle === handle) {
          currentHandle = null;
          LAppDelegate.releaseInstance();
        }
      },
      setLipSync(value: number): void {
        if (active) delegate.setLipSync(value);
      },
      setSpeaking(speaking: boolean): void {
        if (active) delegate.setSpeaking(speaking);
      },
      setExpression(expression: string): boolean {
        return active && delegate.setExpression(expression);
      },
      setEmotion(emotion: string): void {
        if (active) delegate.setEmotion(emotion);
      },
      playMotion(group: string, index?: number): boolean {
        return active && delegate.playMotion(group, index);
      }
    };
    currentHandle = handle;

    const delegate = LAppDelegate.getInstance();
    if (!delegate.initialize(canvas, modelUrl.href, options.transparent)) {
      handle.destroy();
      throw new Error('Unable to initialize WebGL for Live2D');
    }
    delegate.run();

    try {
      await delegate.waitUntilReady();
    } catch (error) {
      handle.destroy();
      throw error;
    }
    if (!active) throw new Error('Live2D runtime was replaced before the model loaded');
    return handle;
  }
};

window.dispatchEvent(new Event('aria-live2d-runtime-ready'));

declare global {
  interface Window {
    Live2DCubismCore?: unknown;
    AriaLive2DRuntime?: {
      mount(canvas: HTMLCanvasElement, options: RuntimeOptions): Promise<RuntimeHandle>;
    };
  }
}
