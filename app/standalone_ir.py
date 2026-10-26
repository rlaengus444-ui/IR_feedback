#!/usr/bin/env python3
"""Standalone BinderJet UI extension. Does not modify the original application."""

from pathlib import Path
import queue
import sys
import threading
import time
import traceback

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from session import SessionController, SessionOptions, default_adapter_factory
from single_instance import SingleInstance


MODE_LABELS = {"조회": "monitor", "계산 미리보기": "preview", "UI 자동 적용": "control"}


def write_error_log(message):
    path = Path(__file__).resolve().with_name("IR-module-error.log")
    try:
        previous = b""
        if path.exists():
            with path.open("rb") as source:
                source.seek(max(0, path.stat().st_size - 65536))
                previous = source.read(65536)
        value = (time.strftime("%Y-%m-%d %H:%M:%S ") + str(message) + "\n").encode("utf-8")
        path.write_bytes((previous + value)[-131072:])
    except OSError:
        pass


def discover_windows():
    adapter = default_adapter_factory()
    try:
        return adapter.discover()
    finally:
        adapter.disconnect()


class ThermalFeedbackWindow:
    CLOSE_GRACE_SECONDS = 4.0

    def __init__(self, root, controller=None, discoverer=discover_windows, auto_discover=True):
        self.root = root
        self.controller = controller or SessionController(error_logger=write_error_log)
        self.discoverer = discoverer
        self.targets = []
        self.target_map = {}
        self.discovery_queue = queue.Queue(maxsize=1)
        self.searching = False
        self.discovery_thread = None
        self.closing = False
        self.close_deadline = None
        self.configuration_widgets = []
        self.machine = tk.StringVar(value="M1")
        self.host = tk.StringVar()
        self.mode = tk.StringVar(value="조회")
        self.target_temperature = tk.StringVar(value="65")
        self.settings_path = tk.StringVar()
        self.status = tk.StringVar(value="연결 안 됨")
        self.capability = tk.StringVar(value="기존 프로그램 무수정 · UI 자동 적용은 선택 후 연결해야 시작됩니다.")
        self.layer = tk.StringVar(value="—")
        self.temperature = tk.StringVar(value="—")
        self.process_state = tk.StringVar(value="—")
        self.decision = tk.StringVar(value="연결 후 새 T_recoating 기록을 기다립니다.")
        self.root.title("IR 피드백 외부 모듈 · CSV")
        self.root.geometry("1080x740")
        self.root.minsize(940, 650)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self._refresh_buttons()
        self.root.after(50, self._pump)
        if auto_discover:
            self.root.after(100, self.search)

    def _build(self):
        style = ttk.Style(self.root)
        style.configure("Title.TLabel", font=("Segoe UI", 20, "bold"))
        style.configure("Metric.TLabel", font=("Segoe UI", 27, "bold"))
        style.configure("Status.TLabel", font=("Segoe UI", 11, "bold"))
        main = ttk.Frame(self.root, padding=22)
        main.pack(fill="both", expand=True)
        ttk.Label(main, text="IR 피드백 확장 모듈", style="Title.TLabel").pack(anchor="w")
        ttk.Label(main, text="원본 무수정 · Monitoring CSV 저장 후 수신 · Process 표 접근 없음", foreground="#536173").pack(anchor="w", pady=(4, 15))

        connection = ttk.LabelFrame(main, text="프로그램 연결", padding=12)
        connection.pack(fill="x")
        ttk.Label(connection, text="장비").grid(row=0, column=0, sticky="w")
        self.machine_box = ttk.Combobox(connection, textvariable=self.machine, values=("M1", "S3"), state="readonly", width=7)
        self.machine_box.grid(row=1, column=0, padx=(0, 12), sticky="w")
        self.machine_box.bind("<<ComboboxSelected>>", lambda event: self._populate_hosts())
        ttk.Label(connection, text="자동 검색한 실행 창").grid(row=0, column=1, sticky="w")
        self.host_box = ttk.Combobox(connection, textvariable=self.host, state="readonly", width=58)
        self.host_box.grid(row=1, column=1, sticky="ew", padx=(0, 10))
        self.search_button = ttk.Button(connection, text="다시 검색", command=self.search)
        self.search_button.grid(row=1, column=2, padx=(0, 8))
        self.connect_button = ttk.Button(connection, text="연결", command=self.connect)
        self.connect_button.grid(row=1, column=3, padx=(0, 8))
        self.disconnect_button = ttk.Button(connection, text="연결 해제", command=self.disconnect)
        self.disconnect_button.grid(row=1, column=4)
        connection.columnconfigure(1, weight=1)
        self.configuration_widgets.extend(((self.machine_box, "readonly"), (self.host_box, "readonly")))

        options = ttk.Frame(connection)
        options.grid(row=2, column=0, columnspan=5, sticky="ew", pady=(12, 0))
        ttk.Label(options, text="모드").pack(side="left")
        self.mode_box = ttk.Combobox(options, textvariable=self.mode, values=tuple(MODE_LABELS), state="readonly", width=16)
        self.mode_box.pack(side="left", padx=(8, 20))
        ttk.Label(options, text="목표 온도 °C").pack(side="left")
        self.temperature_entry = ttk.Entry(options, textvariable=self.target_temperature, width=9)
        self.temperature_entry.pack(side="left", padx=(8, 15))
        ttk.Label(options, text="원본 Excel 기본값 사용 · 시작 시 제어 자동 활성화 안 함", foreground="#536173").pack(side="left")
        self.configuration_widgets.extend(((self.mode_box, "readonly"), (self.temperature_entry, "normal")))
        advanced = ttk.Frame(connection)
        advanced.grid(row=3, column=0, columnspan=5, sticky="ew", pady=(10, 0))
        ttk.Label(advanced, text="설정 파일(선택)").pack(side="left")
        self.settings_entry = ttk.Entry(advanced, textvariable=self.settings_path)
        self.settings_entry.pack(side="left", fill="x", expand=True, padx=8)
        self.browse_button = ttk.Button(advanced, text="찾아보기", command=self.browse)
        self.browse_button.pack(side="left")
        self.configuration_widgets.extend(((self.settings_entry, "normal"), (self.browse_button, "normal")))

        status_row = ttk.Frame(main)
        status_row.pack(fill="x", pady=(15, 5))
        ttk.Label(status_row, textvariable=self.status, style="Status.TLabel").pack(side="left")
        ttk.Label(main, textvariable=self.capability, wraplength=990, foreground="#9a4c10").pack(fill="x", anchor="w", pady=(0, 14))
        metrics = ttk.Frame(main)
        metrics.pack(fill="x")
        for title, variable in (("최근 완료 층", self.layer), ("T_recoating (CSV)", self.temperature), ("프로그램 상태", self.process_state)):
            card = ttk.LabelFrame(metrics, text=title, padding=12)
            card.pack(side="left", fill="both", expand=True, padx=(0, 10))
            ttk.Label(card, textvariable=variable, style="Metric.TLabel").pack(anchor="w")
        self.outputs = ttk.Treeview(main, columns=("setting", "recommended", "scope"), height=2, show="tree headings")
        self.outputs.heading("#0", text="IR")
        self.outputs.heading("setting", text="현재 설정 읽기 (%)")
        self.outputs.heading("recommended", text="계산 권장값 (%)")
        self.outputs.heading("scope", text="설정 범위")
        self.outputs.column("#0", width=125)
        for column in ("setting", "recommended", "scope"):
            self.outputs.column(column, width=220, anchor="center")
        self.outputs.insert("", "end", iid="heater1", text="IR 1", values=("—", "—", "—"))
        self.outputs.insert("", "end", iid="heater2", text="IR 2", values=("—", "—", "—"))
        self.outputs.pack(fill="x", pady=(15, 8))
        ttk.Label(main, textvariable=self.decision, wraplength=990).pack(fill="x", pady=(0, 12))
        ttk.Label(main, text="상태 기록").pack(anchor="w")
        self.log = scrolledtext.ScrolledText(main, height=7, wrap="word", font=("Segoe UI", 9), state="disabled")
        self.log.pack(fill="both", expand=True, pady=(5, 0))

    def browse(self):
        value = filedialog.askopenfilename(parent=self.root, title="알고리즘 설정(선택)", filetypes=(("JSON 설정", "*.json"),))
        if value:
            self.settings_path.set(value)

    def search(self):
        if self.closing or self.searching or self.controller.thread is not None:
            return
        self.searching = True
        self.status.set("프로그램 검색 중…")
        self._refresh_buttons()
        def work():
            try:
                value = (self.discoverer(), None)
            except Exception as error:
                value = ([], str(error))
            try:
                self.discovery_queue.put_nowait(value)
            except queue.Full:
                pass
        self.discovery_thread = threading.Thread(target=work, name="IR-window-discovery", daemon=True)
        self.discovery_thread.start()

    def _populate_hosts(self):
        previous = self.host.get()
        self.target_map = {}
        for target in self.targets:
            if target.machine not in (self.machine.get(), "unknown"):
                continue
            family = "장비 미확인" if target.machine == "unknown" else target.machine
            label = f"{family} · {target.title[:64]} · PID {target.pid}"
            self.target_map[label] = target
        labels = list(self.target_map)
        self.host_box.configure(values=labels)
        self.host.set(previous if previous in self.target_map else labels[0] if labels else "")
        self._refresh_buttons()

    def connect(self):
        target = self.target_map.get(self.host.get())
        if target is None:
            self._append_log("실행 중인 프로그램을 검색하고 창을 선택하세요.")
            return
        try:
            options = SessionOptions(target=target, mode=MODE_LABELS[self.mode.get()],
                target_temperature_c=float(self.target_temperature.get()),
                settings_path=Path(self.settings_path.get()) if self.settings_path.get().strip() else None)
            if self.controller.connect(options):
                self.status.set("연결 중…")
                self.capability.set("CSV 저장 후 자동 적용 · 다른 탭/최소화 가능 · 수동 편집/원본 UI 응답 실패 시 적용 정지"
                    if options.mode == "control" else "읽기·제어 가능 여부를 확인하고 있습니다.")
        except (ValueError, KeyError, OSError) as error:
            self.status.set("연결하지 않음")
            self._append_log(str(error))
        self._refresh_buttons()

    def disconnect(self):
        self.controller.disconnect()
        self.status.set("연결 해제 중…")
        self._refresh_buttons()

    def _refresh_buttons(self):
        busy = self.controller.thread is not None or self.closing
        for widget, idle_state in self.configuration_widgets:
            widget.configure(state="disabled" if busy else idle_state)
        self.search_button.configure(state="disabled" if busy or self.searching else "normal")
        self.connect_button.configure(state="disabled" if busy or self.searching or not self.target_map else "normal")
        self.disconnect_button.configure(state="normal" if busy and not self.closing and self.controller.state != "disconnecting" else "disabled")

    def _append_log(self, message):
        value = str(message)[:4000]
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S ") + value + "\n")
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 101:
            self.log.delete("1.0", f"{lines - 100}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _show_snapshot(self, payload):
        snapshot = payload["snapshot"]
        self.layer.set("—" if snapshot.layer_index is None else f"{snapshot.layer_index + 1} 층")
        self.temperature.set("—" if snapshot.t_recoating_c is None else f"{snapshot.t_recoating_c:.1f} °C")
        self.process_state.set(snapshot.machine_state)
        unavailable = not snapshot.control_available
        self.capability.set(("제어 불가 · " + snapshot.control_unavailable_reason) if unavailable else
            "CSV 자동 적용 중 · Process 표 접근 없음 · 다른 탭/최소화 가능"
            if payload["effective_mode"] == "control" else "기존 Sequences IR + Save 경로 확인됨")
        self.status.set("연결됨 · 제어 불가" if payload["effective_mode"] == "control-unavailable" else
                        "연결됨 · " + {"monitor": "조회", "preview": "계산 미리보기", "control": "UI 자동 적용", "waiting": "자동 상태 대기"}[payload["effective_mode"]])
        for heater, value in ((1, snapshot.heater1_setting_percent), (2, snapshot.heater2_setting_percent)):
            proposed = payload["proposed"].get(1 if snapshot.coupled_output else heater)
            self.outputs.item(f"heater{heater}", values=("—" if value is None else value,
                "—" if proposed is None else proposed, "공통 IR 설정" if snapshot.coupled_output else "개별 설정"))
        reasons = " / ".join(dict.fromkeys(payload["reasons"].values()))
        self.decision.set(reasons or (payload["status"] + (" · " + snapshot.details if snapshot.details else "")))

    def _pump(self):
        if self.closing:
            discovering = self.discovery_thread is not None and self.discovery_thread.is_alive()
            if (not self.controller.worker_alive and not discovering) or time.monotonic() >= self.close_deadline:
                self.root.destroy()
                return
            self.root.after(50, self._pump)
            return
        try:
            targets, error = self.discovery_queue.get_nowait()
        except queue.Empty:
            pass
        else:
            self.searching = False
            self.targets = targets
            known = [target.machine for target in targets if target.machine in ("M1", "S3")]
            if self.machine.get() not in known and len(set(known)) == 1:
                self.machine.set(known[0])
            self._populate_hosts()
            self.status.set("연결 안 됨")
            self._append_log(error or f"실행 창 {len(targets)}개 검색. 연결 버튼으로 시작하세요.")
            if not targets:
                self.capability.set(error or "BinderJet 실행 창을 찾지 못했습니다. 프로그램을 실행한 뒤 다시 검색하세요.")
        for event in self.controller.poll():
            if event.kind == "connected":
                self.status.set("연결됨 · 상태 확인 중")
            elif event.kind == "snapshot":
                self._show_snapshot(event.payload)
            elif event.kind == "log":
                self._append_log(event.payload)
            elif event.kind == "error":
                self.status.set("연결 오류")
                self.capability.set(str(event.payload))
                self._append_log(event.payload)
            elif event.kind == "finished":
                self.status.set("연결 오류" if self.controller.error else "연결 해제됨")
                self._append_log("외부 모듈 연결 종료. 원본 프로그램의 가열 ON/OFF는 변경하지 않습니다.")
        self._refresh_buttons()
        self.root.after(50, self._pump)

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.controller.close()
        self.close_deadline = time.monotonic() + self.CLOSE_GRACE_SECONDS
        self.status.set("종료 중…")
        self._refresh_buttons()


def main():
    def record_error(exc_type, value, trace):
        message = "".join(traceback.format_exception(exc_type, value, trace))
        write_error_log(message)
        try:
            messagebox.showerror("IR 외부 모듈 오류", str(value)[:1500])
        except tk.TclError:
            pass
    sys.excepthook = record_error
    instance = SingleInstance()
    if not instance.acquire():
        messagebox.showinfo("IR 피드백 외부 모듈", "이미 실행 중입니다. 열려 있는 외부 모듈 창을 사용하세요.")
        return
    try:
        root = tk.Tk()
        root.report_callback_exception = record_error
        ThermalFeedbackWindow(root)
        root.mainloop()
    finally:
        instance.close()


if __name__ == "__main__":
    main()
