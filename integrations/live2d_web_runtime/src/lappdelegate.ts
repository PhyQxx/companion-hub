/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */

import { CubismFramework, Option } from '@framework/live2dcubismframework';
import * as LAppDefine from './lappdefine';
import { LAppPal } from './lapppal';
import { LAppSubdelegate } from './lappsubdelegate';
import { CubismLogError } from '@framework/utils/cubismdebug';

export let s_instance: LAppDelegate = null;

/**
 * アプリケーションクラス。
 * Cubism SDKの管理を行う。
 */
export class LAppDelegate {
  /**
   * クラスのインスタンス（シングルトン）を返す。
   * インスタンスが生成されていない場合は内部でインスタンスを生成する。
   *
   * @return クラスのインスタンス
   */
  public static getInstance(): LAppDelegate {
    if (s_instance == null) {
      s_instance = new LAppDelegate();
    }

    return s_instance;
  }

  /**
   * クラスのインスタンス（シングルトン）を解放する。
   */
  public static releaseInstance(): void {
    if (s_instance != null) {
      s_instance.release();
    }

    s_instance = null;
  }

  /**
   * ポインタがアクティブになるときに呼ばれる。
   */
  private onPointerBegan(e: PointerEvent): void {
    for (let i = 0; i < this._subdelegates.length; i++) {
      this._subdelegates[i].onPointBegan(e.pageX, e.pageY);
    }
  }

  /**
   * ポインタが動いたら呼ばれる。
   */
  private onPointerMoved(e: PointerEvent): void {
    for (let i = 0; i < this._subdelegates.length; i++) {
      this._subdelegates[i].onPointMoved(e.pageX, e.pageY);
    }
  }

  /**
   * ポインタがアクティブでなくなったときに呼ばれる。
   */
  private onPointerEnded(e: PointerEvent): void {
    for (let i = 0; i < this._subdelegates.length; i++) {
      this._subdelegates[i].onPointEnded(e.pageX, e.pageY);
    }
  }

  /**
   * ポインタがキャンセルされると呼ばれる。
   */
  private onPointerCancel(e: PointerEvent): void {
    for (let i = 0; i < this._subdelegates.length; i++) {
      this._subdelegates[i].onTouchCancel(e.pageX, e.pageY);
    }
  }

  /**
   * Resize canvas and re-initialize view.
   */
  public onResize(): void {
    for (let i = 0; i < this._subdelegates.length; i++) {
      this._subdelegates[i].onResize();
    }
  }

  /**
   * 実行処理。
   */
  public run(): void {
    // メインループ
    const loop = (): void => {
      // インスタンスの有無の確認
      if (s_instance == null) {
        return;
      }

      // 時間更新
      LAppPal.updateTime();

      for (let i = 0; i < this._subdelegates.length; i++) {
        this._subdelegates[i].update();
      }

      // ループのために再帰呼び出し
      this._animationFrame = requestAnimationFrame(loop);
    };
    loop();
  }

  public waitUntilReady(timeoutMs = 15000): Promise<void> {
    const startedAt = performance.now();
    return new Promise((resolve, reject) => {
      const check = (): void => {
        if (s_instance !== this) {
          reject(new Error('Live2D runtime was released before the model loaded'));
          return;
        }
        if (this._subdelegates[0]?.isReady()) {
          resolve();
          return;
        }
        if (performance.now() - startedAt >= timeoutMs) {
          reject(new Error('Timed out while loading the Live2D model'));
          return;
        }
        requestAnimationFrame(check);
      };
      check();
    });
  }

  public setLipSync(value: number): void {
    this._subdelegates[0]?.getLive2DManager().setLipSync(value);
  }

  public setSpeaking(active: boolean): void {
    this._subdelegates[0]?.getLive2DManager().setSpeaking(active);
  }

  public setExpression(expression: string): boolean {
    return this._subdelegates[0]?.getLive2DManager().setExpression(expression) ?? false;
  }

  public setEmotion(emotion: string): void {
    this._subdelegates[0]?.getLive2DManager().setEmotion(emotion);
  }

  public playMotion(group: string, index?: number): boolean {
    return this._subdelegates[0]?.getLive2DManager().playMotion(group, index) ?? false;
  }

  /**
   * 解放する。
   */
  private release(): void {
    this.releaseEventListener();
    this.releaseSubdelegates();

    // Cubism SDKの解放
    CubismFramework.dispose();

    this._cubismOption = null;
  }

  /**
   * イベントリスナーを解除する。
   */
  private releaseEventListener(): void {
    if (!this._eventTarget) return;
    this._eventTarget.removeEventListener('pointerdown', this.pointBeganEventListener);
    this.pointBeganEventListener = null;
    this._eventTarget.removeEventListener('pointermove', this.pointMovedEventListener);
    this.pointMovedEventListener = null;
    this._eventTarget.removeEventListener('pointerup', this.pointEndedEventListener);
    this.pointEndedEventListener = null;
    this._eventTarget.removeEventListener('pointercancel', this.pointCancelEventListener);
    this.pointCancelEventListener = null;
    this._eventTarget = null;
    if (this._animationFrame != null) cancelAnimationFrame(this._animationFrame);
    this._animationFrame = null;
  }

  /**
   * Subdelegate を解放する
   */
  private releaseSubdelegates(): void {
    if (!this._subdelegates) return;
    for (let i = 0; i < this._subdelegates.length; i++) {
      this._subdelegates[i].release();
    }

    this._subdelegates.length = 0;
    this._subdelegates = null;
  }

  /**
   * APPに必要な物を初期化する。
   */
  public initialize(
    canvas: HTMLCanvasElement,
    modelUrl: string,
    _transparent: boolean
  ): boolean {
    // Cubism SDKの初期化
    this.initializeCubism();

    if (!this.initializeSubdelegates(canvas, modelUrl)) return false;
    this.initializeEventListener(canvas);

    return true;
  }

  /**
   * イベントリスナーを設定する。
   */
  private initializeEventListener(canvas: HTMLCanvasElement): void {
    this.pointBeganEventListener = this.onPointerBegan.bind(this);
    this.pointMovedEventListener = this.onPointerMoved.bind(this);
    this.pointEndedEventListener = this.onPointerEnded.bind(this);
    this.pointCancelEventListener = this.onPointerCancel.bind(this);

    // ポインタ関連コールバック関数登録
    this._eventTarget = canvas;
    canvas.addEventListener('pointerdown', this.pointBeganEventListener, {
      passive: true
    });
    canvas.addEventListener('pointermove', this.pointMovedEventListener, {
      passive: true
    });
    canvas.addEventListener('pointerup', this.pointEndedEventListener, {
      passive: true
    });
    canvas.addEventListener('pointercancel', this.pointCancelEventListener, {
      passive: true
    });
  }

  /**
   * Cubism SDKの初期化
   */
  private initializeCubism(): void {
    LAppPal.updateTime();

    // setup cubism
    this._cubismOption.logFunction = LAppPal.printMessage;
    this._cubismOption.loggingLevel = LAppDefine.CubismLoggingLevel;
    CubismFramework.startUp(this._cubismOption);

    // initialize cubism
    CubismFramework.initialize();
  }

  /**
   * Canvasを生成配置、Subdelegateを初期化する
   */
  private initializeSubdelegates(
    canvas: HTMLCanvasElement,
    modelUrl: string
  ): boolean {
    this._canvases = [canvas];
    const subdelegate = new LAppSubdelegate();
    if (!subdelegate.initialize(canvas, modelUrl) || subdelegate.isContextLost()) {
      CubismLogError('Unable to initialize the Live2D WebGL canvas.');
      return false;
    }
    this._subdelegates = [subdelegate];
    return true;
  }

  /**
   * Privateなコンストラクタ
   */
  private constructor() {
    this._cubismOption = new Option();
    this._subdelegates = new Array<LAppSubdelegate>();
    this._canvases = new Array<HTMLCanvasElement>();
    this._eventTarget = null;
    this._animationFrame = null;
  }

  /**
   * Cubism SDK Option
   */
  private _cubismOption: Option;

  /**
   * 操作対象のcanvas要素
   */
  private _canvases: Array<HTMLCanvasElement>;

  /**
   * Subdelegate
   */
  private _subdelegates: Array<LAppSubdelegate>;

  private _eventTarget: HTMLCanvasElement | null;

  private _animationFrame: number | null;

  /**
   * 登録済みイベントリスナー 関数オブジェクト
   */
  private pointBeganEventListener: (ev: PointerEvent) => void;

  /**
   * 登録済みイベントリスナー 関数オブジェクト
   */
  private pointMovedEventListener: (ev: PointerEvent) => void;

  /**
   * 登録済みイベントリスナー 関数オブジェクト
   */
  private pointEndedEventListener: (ev: PointerEvent) => void;

  /**
   * 登録済みイベントリスナー 関数オブジェクト
   */
  private pointCancelEventListener: (ev: PointerEvent) => void;
}
