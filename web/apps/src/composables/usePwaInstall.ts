import { type Ref } from "vue";

export interface BeforeInstallPromptEvent extends Event {
  prompt(): Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed"; platform: string }>;
}

/** PWA 安装提示：拦截 beforeinstallprompt 并在用户点击安装时调用原生提示。 */
export function usePwaInstall(installPrompt: Ref<BeforeInstallPromptEvent | null>) {
  function handleBeforeInstallPrompt(event: Event) {
    event.preventDefault();
    installPrompt.value = event as BeforeInstallPromptEvent;
  }

  async function installPwa() {
    const prompt = installPrompt.value;
    if (!prompt) return;
    await prompt.prompt();
    await prompt.userChoice;
    installPrompt.value = null;
  }

  return { handleBeforeInstallPrompt, installPwa };
}
