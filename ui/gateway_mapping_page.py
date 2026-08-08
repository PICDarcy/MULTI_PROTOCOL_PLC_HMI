"""Gateway Canonical Tag 與雙輸出映射管理頁。"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from core.gateway_mapping_manager import GatewayMappingManager


_MISSING = object()


class GatewayMappingPage(ttk.Frame):
    """顯示並編輯穩定 Tag、Modbus 與 OPC UA 輸出設定。"""

    COLUMNS = (
        "name",
        "type",
        "source",
        "state",
        "enabled",
        "modbus",
        "opcua",
    )

    def __init__(self, parent, app_context):
        super().__init__(parent)
        self.app_context = app_context
        self.config_manager = self._context_get(app_context, "config_manager")
        self.log_func = self._context_get(
            app_context,
            "log_func",
            lambda message, level="INFO": None,
        )
        runtime = self._context_get(app_context, "gateway_runtime", None)
        reload_callback = getattr(runtime, "reload", None)
        self.manager = GatewayMappingManager(
            self.config_manager,
            reload_callback=reload_callback if callable(reload_callback) else None,
        )
        self._tags_by_id: dict[str, Any] = {}
        self.status_var = tk.StringVar(value="尚未載入")
        self._build_ui()
        self.refresh()

    @staticmethod
    def _context_get(
        app_context: Any,
        key: str,
        default: Any = _MISSING,
    ) -> Any:
        if isinstance(app_context, dict) and key in app_context:
            return app_context[key]
        if hasattr(app_context, key):
            return getattr(app_context, key)
        if default is not _MISSING:
            return default
        raise KeyError(f"app_context缺少必要項目：{key}")

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = ttk.Frame(self, padding=(14, 12, 14, 6))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(
            header,
            text="Gateway Tag映射",
            font=("Microsoft JhengHei UI", 16, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text=(
                "修改顯示名稱與兩種輸出；穩定 Tag ID、來源識別與既有固定位址"
                "不會因重新掃描而改變。"
            ),
        ).grid(row=1, column=0, sticky="w", pady=(3, 0))

        actions = ttk.Frame(header)
        actions.grid(row=0, column=1, rowspan=2, sticky="e")
        ttk.Button(actions, text="重新整理", command=self.refresh).grid(
            row=0, column=0, padx=4
        )
        ttk.Button(actions, text="編輯選取 Tag", command=self.edit_selected).grid(
            row=0, column=1, padx=4
        )

        table_frame = ttk.Frame(self, padding=(14, 6, 14, 6))
        table_frame.grid(row=1, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            table_frame,
            columns=self.COLUMNS,
            show="tree headings",
            selectmode="browse",
        )
        self.tree.heading("#0", text="Tag ID")
        headings = {
            "name": "顯示名稱",
            "type": "型別",
            "source": "來源位址",
            "state": "來源／映射狀態",
            "enabled": "Tag",
            "modbus": "Modbus TCP輸出",
            "opcua": "OPC UA輸出",
        }
        widths = {
            "#0": 235,
            "name": 150,
            "type": 80,
            "source": 190,
            "state": 130,
            "enabled": 60,
            "modbus": 250,
            "opcua": 200,
        }
        self.tree.column("#0", width=widths["#0"], minwidth=140)
        for column in self.COLUMNS:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], minwidth=60)

        y_scroll = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=self.tree.yview,
        )
        x_scroll = ttk.Scrollbar(
            table_frame,
            orient="horizontal",
            command=self.tree.xview,
        )
        self.tree.configure(
            yscrollcommand=y_scroll.set,
            xscrollcommand=x_scroll.set,
        )
        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<Double-1>", lambda _event: self.edit_selected())

        ttk.Label(
            self,
            textvariable=self.status_var,
            anchor="w",
            padding=(14, 4, 14, 10),
        ).grid(row=2, column=0, sticky="ew")

    def refresh(self) -> None:
        selected = self._selected_tag_id()
        model = self.manager.get_model()
        self._tags_by_id = {str(tag.tag_id): tag for tag in model.tags}
        self.tree.delete(*self.tree.get_children())
        for tag in sorted(
            model.tags,
            key=lambda item: (
                str(item.device_id),
                item.name.casefold(),
                str(item.tag_id),
            ),
        ):
            tag_id = str(tag.tag_id)
            metadata = dict(tag.to_dict().get("metadata", {}))
            source_state = str(metadata.get("source_state", "online"))
            mapping_state = str(metadata.get("mapping_state", "confirmed"))
            self.tree.insert(
                "",
                "end",
                iid=tag_id,
                text=tag_id,
                values=(
                    tag.name,
                    tag.data_type,
                    tag.source_address,
                    f"{source_state}／{mapping_state}",
                    "啟用" if tag.enabled else "停用",
                    self._modbus_text(tag),
                    self._opcua_text(tag),
                ),
            )
        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)
            self.tree.focus(selected)
            self.tree.see(selected)
        self.status_var.set(f"共 {len(model.tags)} 個 Canonical Tag")

    @staticmethod
    def _modbus_text(tag) -> str:
        mapping = tag.modbus_tcp_output
        if not mapping.supported:
            return f"不支援：{mapping.unsupported_reason}"
        address = "自動" if mapping.address is None else str(mapping.address)
        state = "發布" if mapping.enabled else "停用"
        return (
            f"{state} {mapping.area} {address} "
            f"({mapping.byte_order}/{mapping.word_order})"
        )

    @staticmethod
    def _opcua_text(tag) -> str:
        mapping = tag.opcua_output
        state = "發布" if mapping.enabled else "停用"
        return f"{state} {mapping.browse_name or tag.name}"

    def _selected_tag_id(self) -> str:
        selection = self.tree.selection() if hasattr(self, "tree") else ()
        return str(selection[0]) if selection else ""

    def edit_selected(self) -> None:
        tag_id = self._selected_tag_id()
        if not tag_id:
            messagebox.showinfo("Gateway Tag映射", "請先選取一個 Tag。", parent=self)
            return
        tag = self._tags_by_id.get(tag_id)
        if tag is None:
            self.refresh()
            return
        editor = _MappingEditor(self, tag)
        values = editor.show()
        if values is None:
            return
        try:
            self.manager.update_tag(tag_id, **values)
        except Exception as exc:
            self._log(f"Gateway Tag映射保存失敗：{exc}", "ERROR")
            messagebox.showerror("無法保存映射", str(exc), parent=self)
            return
        self._log(f"Gateway Tag映射已保存：{tag_id}", "INFO")
        self.refresh()
        self.status_var.set(f"已保存 {tag_id} 並重新載入 Gateway 輸出")

    def _log(self, message: str, level: str = "INFO") -> None:
        if not callable(self.log_func):
            return
        try:
            self.log_func(message, level)
        except TypeError:
            self.log_func(message)


class _MappingEditor:
    """單一 Tag 的模態編輯視窗。"""

    def __init__(self, parent: GatewayMappingPage, tag) -> None:
        self.parent = parent
        self.tag = tag
        self.result: dict[str, Any] | None = None
        self.window = tk.Toplevel(parent)
        self.window.title(f"編輯映射－{tag.name}")
        self.window.transient(parent.winfo_toplevel())
        self.window.resizable(False, False)

        mapping = tag.modbus_tcp_output
        opcua = tag.opcua_output
        self.name_var = tk.StringVar(value=tag.name)
        self.enabled_var = tk.BooleanVar(value=tag.enabled)
        self.publish_modbus_var = tk.BooleanVar(value=mapping.enabled)
        self.modbus_address_var = tk.StringVar(
            value="" if mapping.address is None else str(mapping.address)
        )
        self.byte_order_var = tk.StringVar(value=mapping.byte_order)
        self.word_order_var = tk.StringVar(value=mapping.word_order)
        self.publish_opcua_var = tk.BooleanVar(value=opcua.enabled)
        self.opcua_name_var = tk.StringVar(value=opcua.browse_name or tag.name)
        self._build_ui()

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.window, padding=14)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text=f"Tag ID：{self.tag.tag_id}").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8)
        )
        ttk.Label(frame, text=f"來源：{self.tag.source_address}").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )

        ttk.Label(frame, text="顯示名稱").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.name_var, width=38).grid(
            row=2, column=1, sticky="ew", pady=4
        )
        ttk.Checkbutton(
            frame,
            text="啟用 Tag",
            variable=self.enabled_var,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=4)

        modbus = ttk.LabelFrame(frame, text="Modbus TCP輸出", padding=10)
        modbus.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 5))
        modbus.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            modbus,
            text="發布至 Modbus TCP",
            variable=self.publish_modbus_var,
            state="normal" if self.tag.modbus_tcp_output.supported else "disabled",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=3)
        if not self.tag.modbus_tcp_output.supported:
            ttk.Label(
                modbus,
                text=self.tag.modbus_tcp_output.unsupported_reason,
            ).grid(row=1, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Label(modbus, text="起始位址（留白＝自動）").grid(
            row=2, column=0, sticky="w", pady=3
        )
        ttk.Entry(modbus, textvariable=self.modbus_address_var, width=16).grid(
            row=2, column=1, sticky="ew", pady=3
        )
        ttk.Label(modbus, text="Byte Order").grid(row=3, column=0, sticky="w", pady=3)
        ttk.Combobox(
            modbus,
            textvariable=self.byte_order_var,
            values=("big", "little"),
            state="readonly",
            width=12,
        ).grid(row=3, column=1, sticky="ew", pady=3)
        ttk.Label(modbus, text="Word Order").grid(row=4, column=0, sticky="w", pady=3)
        ttk.Combobox(
            modbus,
            textvariable=self.word_order_var,
            values=("big", "little"),
            state="readonly",
            width=12,
        ).grid(row=4, column=1, sticky="ew", pady=3)

        opcua = ttk.LabelFrame(frame, text="OPC UA輸出", padding=10)
        opcua.grid(row=5, column=0, columnspan=2, sticky="ew", pady=5)
        opcua.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            opcua,
            text="發布至 OPC UA",
            variable=self.publish_opcua_var,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Label(opcua, text="公開名稱").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(opcua, textvariable=self.opcua_name_var).grid(
            row=1, column=1, sticky="ew", pady=3
        )

        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="取消", command=self.window.destroy).grid(
            row=0, column=0, padx=4
        )
        ttk.Button(buttons, text="保存", command=self._accept).grid(
            row=0, column=1, padx=4
        )
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)

    def _accept(self) -> None:
        address_text = self.modbus_address_var.get().strip()
        try:
            address: int | None = None if not address_text else int(address_text)
        except ValueError:
            messagebox.showerror(
                "位址格式錯誤",
                "Modbus起始位址必須是0至65535的整數，或留白使用自動配置。",
                parent=self.window,
            )
            return
        self.result = {
            "name": self.name_var.get(),
            "enabled": self.enabled_var.get(),
            "publish_modbus": self.publish_modbus_var.get(),
            "modbus_address": address,
            "modbus_byte_order": self.byte_order_var.get(),
            "modbus_word_order": self.word_order_var.get(),
            "publish_opcua": self.publish_opcua_var.get(),
            "opcua_browse_name": self.opcua_name_var.get(),
        }
        self.window.destroy()

    def show(self) -> dict[str, Any] | None:
        self.window.grab_set()
        self.window.wait_window()
        return self.result
