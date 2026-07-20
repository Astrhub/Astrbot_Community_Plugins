import { useDialog } from "naive-ui";

export function useExternalOpenConfirm() {
  const dialog = useDialog();

  function confirmExternalOpen(url: string): void {
    if (!url) return;
    dialog.info({
      title: "即将打开外链",
      content: `将跳转到：${url}`,
      positiveText: "继续打开",
      negativeText: "取消",
      onPositiveClick: () => window.open(url, "_blank", "noopener,noreferrer"),
    });
  }

  return { confirmExternalOpen };
}
