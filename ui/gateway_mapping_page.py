"""Canonical Tag 與雙輸出映射管理頁。"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any


class _TagMappingDialog(tk.Toplevel):
    def __init__(self, parent, tag) -> None:
        super().__init__(parent)
        self.title(f"修改 Tag 映射：{tag.tag_id}")
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)
        self.result: dict[str, Any] | None = None

        self.name_var = tk.StringVar(value=tag.name)
        self.enabled_var = tk.BooleanVar(value=tag.enabled)
        self.publish_modbus_var = tk.BooleanVar(
            value=tag.modbus_tcp_output.enabled
        )
        self.modbus_address_var = tk.StringVar(
            value=(
                ""
                if tag.modbus_tcp_output.address is None
                else str(tag.modbus_tcp_output.address)
            )
        )
        self.byte_order_var = tk.StringVar(
            value=tag.modbus_tcp_output.byte_order
        )
        self.word_order_var = tk.StringVar(
            value=tag.modbus_tcp_output.word_order
        )
        self.publish_opcua_var = tk.BooleanVar(value=tag.opcua_output.enabled)
        self.opcua_name_var = tk.StringVar(
            value=tag.opcua_output.browse_name or tag.name
        )

        body = ttk.Frame(self, padding=14)
        body.grid(sticky="nsew")
        body.columnconfigure(1, weight=1)
        row = 0
        row = self._entry(
            body,
            row,
            "Tag ID（固定）",
            str(tag.tag_id),
            readonly=True,
        )
        row = self._entry(
            body,
            row,
            "來源型別",
            tag.data_type,
            readonly=True,
        )
        row = self._entry(body, row, "顯示名稱", self.name_var)
        row = self._check(body, row, "啟用 Tag", self.enabled_var)
        row = self._check(
            body,
            row,
            "發布至 Modbus TCP",
            self.publish_modbus_var,
        )
        row = self._entry(
            body,
            row,
            "Modbus 區域",
            tag.modbus_tcp_output.area,
            readonly=True,
        )
        row = self._entry(
            body,
            row,
            "Modbus 0-based 位址（留空自動配置）",
            self.modbus_address_var,
        )
        row = self._combo(body, row, "Byte Order", self.byte_order_var)
        row = self._combo(body, row, "Word Order", self.word_order_var)
        row = self._check(
            body,
            row,
            "發布至 OPC UA",
            self.publish_opcua_var,
        )
        row = self._entry(
            body,
            row,
            "OPC UA 公開名稱",
            self.opcua_name_var,
        )

        buttons = ttk.Frame(body)
        buttons.grid(row=row, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="儲存", command=self._save).pack(
            side=tk.LEFT,
            padx=4,
        )
        ttk.Button(buttons, text="取消", command=self.destroy).pack(
            side=tk.LEFT,
            padx=4,
        )
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _event: self.destroy())
        self.grab_set()

    @staticmethod
    def _label(parent, row: int, text: str) -> None:
        ttk.Label(parent, text=text).grid(
            row=row,
            column=0,
            sticky="w",
            padx=(0, 12),
            pady=5,
        )

    def _entry(self, parent, row, label, value, readonly=False) -> int:
        self._label(parent, row, label)
        variable = (
            value
            if isinstance(value, tk.Variable)
            else tk.StringVar(value=value)
        )
        entry = ttk.Entry(parent, textvariable=variable, width=42)
        if readonly:
            entry.configure(state="readonly")
        entry.grid(row=row, column=1, sticky="ew", pady=5)
        return row + 1

    def _check(self, parent, row, label, variable) -> int:
        self._label(parent, row, label)
        ttk.Checkbutton(parent, variable=variable).grid(
            row=row,
            column=1,
            sticky="w",
            pady=5,
        )
        return row + 1

    def _combo(self, parent, row, label, variable) -> int:
        self._label(parent, row, label)
        ttk.Combobox(
            parent,
            textvariable=variable,
            values=("big", "little"),
            state="readonly",
            width=39,
        ).grid(row=row, column=1, sticky="ew", pady=5)
        return row + 1

    def _save(self) -> None:
        address_text = self.modbus_address_var.get().strip()
        try:
            address = None if not address_text else int(address_text)
        except ValueError:
            messagebox.showerror(
                "欄位錯誤",
                "Modbus 位址必須是 0 至 65535 的整數，或留空自動配置。",
                parent=self,
            )
            return
        if address is not None and not 0 <= address <= 65535:
            messagebox.showerror(
                "欄位錯誤",
                "Modbus 位址必須介於 0 至 65535。",
                parent=self,
            )
            return
        self.result = {
            "name": self.name_var.get().strip(),
            "enabled": self.enabled_var.get(),
            "publish_modbus": self.publish_modbus_var.get(),
            "publish_opcua": self.publish_opcua_var.get(),
            "modbus_address": address,
            "byte_order": self.byte_order_var.get(),
            "word_order": self.word_order_var.get(),
            "opcua_browse_name": self.opcua_name_var.get().strip(),
        }
        self.destroy()

    def show(self) -> dict[str, Any] | None:
        self.wait_window()
        return self.result


class GatewayMappingPage(ttk.Frame):
    """讓設定人員管理穩定 Tag 與兩種獨立輸出。"""

    COLUMNS = (
        "online",
        "enabled",
        "name",
        "data_type",
        "modbus",
        "address",
        "order",
        "opcua",
        "opcua_name",
        "node_id",
        "status",
    )

    def __init__(self, parent, app_context) -> None:
        super().__init__(parent)
        self.app_context = app_context
        self.mapping_manager = self._ctx("gateway_mapping_manager")
        self.gateway_runtime = self._ctx("gateway_runtime")
        self.log_func = self._ctx("log_func", print)
        self.status_var = tk.StringVar(value="尚未載入 Canonical Tag")
        self._tags: dict[str, Any] = {}
        self._build_ui()
        self.refresh()

    def _ctx(self, name: str, default=None):
        if isinstance(self.app_context, dict):
            return self.app_context.get(name, default)
        return getattr(self.app_context, name, default)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(self, padding=(8, 8, 8, 4))
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.columnconfigure(0, weight=1)
        ttk.Label(
            toolbar,
            text="Canonical Tag 雙輸出映射",
            font=("TkDefaultFont", 14, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(toolbar, text="重新載入", command=self.refresh).grid(
            row=0,
            column=1,
            padx=4,
        )
        ttk.Button(toolbar, text="修改選取 Tag", command=self._edit).grid(
            row=0,
            column=2,
            padx=4,
        )
        ttk.Button(
            toolbar,
            text="確認偵測型別",
            command=self._confirm_type,
        ).grid(row=0, column=3, padx=4)

        holder = ttk.Frame(self, padding=(8, 4))
        holder.grid(row=1, column=0, sticky="nsew")
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        headings = (
            "來源",
            "啟用",
            "Tag 名稱",
            "型別",
            "Modbus",
            "位址",
            "Byte / Word",
            "OPC UA",
            "公開名稱",
            "穩定 NodeId",
            "映射狀態",
        )
        widths = (55, 55, 150, 80, 70, 70, 100, 70, 150, 170, 130)
        self.tree = ttk.Treeview(
            holder,
            columns=self.COLUMNS,
            show="headings",
            selectmode="browse",
        )
        for column, heading, width in zip(self.COLUMNS, headings, widths):
            self.tree.heading(column, text=heading)
            self.tree.column(
                column,
                width=width,
                minwidth=50,
                anchor="center",
            )
        self.tree.column("name", anchor="w")
        self.tree.column("opcua_name", anchor="w")
        self.tree.bind("<Double-1>", lambda _event: self._edit())
        ybar = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        xbar = ttk.Scrollbar(holder, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")

        ttk.Label(self, textvariable=self.status_var, anchor="w").grid(
            row=2,
            column=0,
            sticky="ew",
            padx=8,
            pady=(4, 8),
        )

    def refresh(self) -> None:
        try:
            model = self.mapping_manager.get_model()
            self._tags = {str(tag.tag_id): tag for tag in model.tags}
            self.tree.delete(*self.tree.get_children(""))
            for tag_id, tag in self._tags.items():
                modbus = tag.modbus_tcp_output
                opcua = tag.opcua_output
                status = (
                    f"待確認 {tag.pending_source_data_type}"
                    if tag.mapping_confirmation_required
                    else "正常"
                )
                self.tree.insert(
                    "",
                    "end",
                    iid=tag_id,
                    values=(
                        "線上" if tag.source_online else "離線",
                        "是" if tag.enabled else "否",
                        tag.name,
                        tag.data_type,
                        "是" if modbus.enabled else "否",
                        "自動" if modbus.address is None else modbus.address,
                        f"{modbus.byte_order} / {modbus.word_order}",
                        "是" if opcua.enabled else "否",
                        opcua.browse_name or tag.name,
                        tag.tag_id,
                        status,
                    ),
                )
            self.status_var.set(f"已載入 {len(self._tags)} 個 Canonical Tag")
        except Exception as exc:
            self._error("載入 Tag 映射失敗", exc)

    def _selected_tag(self):
        selection = self.tree.selection()
        if not selection:
            messagebox.showwarning(
                "尚未選取",
                "請先選取一個 Tag。",
                parent=self,
            )
            return None
        return self._tags.get(selection[0])

    def _edit(self) -> None:
        tag = self._selected_tag()
        if tag is None:
            return
        values = _TagMappingDialog(self, tag).show()
        if values is None:
            return
        try:
            self.mapping_manager.update_tag(str(tag.tag_id), **values)
            self._restart_outputs()
            self.refresh()
            self.status_var.set(f"Tag「{tag.tag_id}」映射已原子保存")
        except Exception as exc:
            self._error("儲存 Tag 映射失敗", exc)

    def _confirm_type(self) -> None:
        tag = self._selected_tag()
        if tag is None:
            return
        if not tag.mapping_confirmation_required:
            messagebox.showinfo(
                "不需確認",
                "選取 Tag 沒有待確認的來源型別。",
                parent=self,
            )
            return
        if not messagebox.askyesno(
            "確認來源型別",
            f"將 {tag.data_type} 改為 {tag.pending_source_data_type}？\n"
            "若新占用範圍與既有映射衝突，設定將保持原狀。",
            parent=self,
        ):
            return
        try:
            self.mapping_manager.confirm_pending_data_type(str(tag.tag_id))
            self._restart_outputs()
            self.refresh()
        except Exception as exc:
            self._error("確認來源型別失敗", exc)

    def _restart_outputs(self) -> None:
        runtime = self.gateway_runtime
        if runtime is None or not runtime.is_running():
            return
        runtime.stop()
        runtime.start()

    def _error(self, title: str, exc: BaseException) -> None:
        self.status_var.set(f"{title}：{exc}")
        try:
            self.log_func(f"{title}：{exc}", "ERROR")
        except TypeError:
            self.log_func(f"{title}：{exc}")
        messagebox.showerror(title, str(exc), parent=self)
