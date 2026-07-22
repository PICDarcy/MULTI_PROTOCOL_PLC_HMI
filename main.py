"""多協定PLC HMI啟動入口。"""

from __future__ import annotations

import traceback


def main() -> None:
    """建立並啟動Tkinter主程式。"""
    app = None
    try:
        from ui.app import App

        app = App()
        app.mainloop()
    except Exception as exc:
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        print("主程式啟動或執行失敗：")
        print(detail)

        try:
            import tkinter as tk
            from tkinter import messagebox

            if app is not None and app.winfo_exists():
                messagebox.showerror(
                    "主程式錯誤",
                    f"主程式啟動或執行失敗：\n{exc}",
                    parent=app,
                )
            else:
                error_root = tk.Tk()
                error_root.withdraw()
                messagebox.showerror(
                    "主程式錯誤",
                    f"主程式啟動或執行失敗：\n{exc}",
                    parent=error_root,
                )
                error_root.destroy()
        except Exception:
            # 無圖形環境時已由print輸出完整錯誤。
            pass
        finally:
            if app is not None:
                try:
                    app.destroy()
                except Exception:
                    pass


if __name__ == "__main__":
    main()
