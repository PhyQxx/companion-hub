import { ElMessage } from "element-plus";
import { withErrorToast } from "@aria/shared";

/**
 * 管理端统一错误提示：失败时 ElMessage.error 显示错误文案并返回 null。
 * 需要额外处理（如状态栏、表单重置）的调用方用 withErrorToast 自注入 onError。
 */
export function withToast<T>(fn: () => Promise<T>, fallback?: string): Promise<T | null> {
  return withErrorToast(fn, (message) => ElMessage.error(message), fallback);
}

/** 同 withToast，另经 onStatus 同步状态栏（典型为 emit("status", message, true)） */
export function withToastStatus<T>(
  fn: () => Promise<T>,
  onStatus: (message: string) => void,
  fallback?: string,
): Promise<T | null> {
  return withErrorToast(
    fn,
    (message) => {
      ElMessage.error(message);
      onStatus(message);
    },
    fallback,
  );
}
