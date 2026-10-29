#!/usr/bin/env python3
"""Standalone BinderJet UI extension. Does not modify the original application."""

from pathlib import Path
import math
import queue
import sys
import threading
import time
import traceback

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from session import SessionController, SessionOptions, default_adapter_factory
from single_instance import SingleInstance
from layer_charts import LayerCharts
from plot_archive import PlotArchive
from target_temperature import parse_target_temperature
from excel_workbook import WorkbookSpec
from slice_panel import SlicePanel


MODE_LABELS = {"UI 자동 적용": "control"}


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

    def __init__(self, root, controller=None, discoverer=discover_windows, auto_discover=True, archive=None):
        self.root = root
        self.controller = controller or SessionController(error_logger=write_error_log)
        self.archive = archive if archive is not None else PlotArchive()
        self._history_generation = 0
        self._history_machine = "M1"
        self._history_target_temperature = None
        self._closing_archive_started = False
        self._closing_charts_finalized = False
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
        self.mode = tk.StringVar(value="UI 자동 적용")
        self.target_temperature = tk.StringVar(value="65")
        self.settings_path = tk.StringVar()
        self.algorithm_type = tk.StringVar(value="기본 알고리즘")
        self.algorithm_path_label = tk.StringVar(value="설정 JSON(선택)")
        self.workbook_sheet = tk.StringVar()
        self.workbook_temperature_cell = tk.StringVar(value="B13")
        self.workbook_result_cell = tk.StringVar(value="D13")
        self.workbook_power_cell = tk.StringVar(value="C13")
        self.workbook_target_cell = tk.StringVar(value="B5")
        self.workbook_layer_cell = tk.StringVar(value="A13")
        self.workbook_slice_cell = tk.StringVar(value="BL13")
        self.status = tk.StringVar(value="연결 안 됨")
        self.capability = tk.StringVar(value="기존 프로그램 무수정 · 연결 버튼을 누르면 UI 자동 적용 준비를 시작합니다.")
        self.layer = tk.StringVar(value="—")
        self.temperature = tk.StringVar(value="—")
        self.process_state = tk.StringVar(value="—")
        self.decision = tk.StringVar(value="연결 후 새 T_recoating 기록을 기다립니다.")
        self.root.title("IR 피드백 외부 모듈 · CSV")
        self.root.geometry("1120x900")
        self.root.minsize(940, 760)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self._refresh_buttons()
        self.root.after(50, self._pump)
        if auto_discover:
            self.root.after(100, self.search)

    def _build(self):
        style = ttk.Style(self.root)
        style.configure("Title.TLabel", font=("Segoe UI", 20, "bold"))
        style.configure("Metric.TLabel", font=("Segoe UI", 23, "bold"))
        style.configure("Status.TLabel", font=("Segoe UI", 11, "bold"))
        main = ttk.Frame(self.root, padding=14)
        main.pack(fill="both", expand=True)
        ttk.Label(main, text="IR 피드백 확장 모듈", style="Title.TLabel").pack(anchor="w")
        ttk.Label(main, text="원본 무수정 · Monitoring CSV 저장 후 수신 · Process 표 접근 없음", foreground="#536173").pack(anchor="w", pady=(3, 8))

        connection = ttk.LabelFrame(main, text="프로그램 연결", padding=8)
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
        self.mode_label = ttk.Label(options, textvariable=self.mode, width=16)
        self.mode_label.pack(side="left", padx=(8, 20))
        ttk.Label(options, text="목표 온도 °C").pack(side="left")
        self.temperature_entry = ttk.Entry(options, textvariable=self.target_temperature, width=9)
        self.temperature_entry.pack(side="left", padx=(8, 5))
        self.target_apply_button = ttk.Button(options, text="목표 적용", command=self.apply_target_temperature)
        self.target_apply_button.pack(side="left", padx=(0, 12))
        ttk.Label(options, text="연결 중 변경 시 다음 새 층부터 반영", foreground="#536173").pack(side="left")
        advanced = ttk.Frame(connection)
        advanced.grid(row=3, column=0, columnspan=5, sticky="ew", pady=(10, 0))
        ttk.Label(advanced, text="알고리즘").pack(side="left")
        self.algorithm_box = ttk.Combobox(advanced, textvariable=self.algorithm_type,
            values=("기본 알고리즘", "Excel 수식 파일"), state="readonly", width=17)
        self.algorithm_box.pack(side="left", padx=(8, 15))
        self.algorithm_box.bind("<<ComboboxSelected>>", self._algorithm_changed)
        ttk.Label(advanced, textvariable=self.algorithm_path_label).pack(side="left")
        self.settings_entry = ttk.Entry(advanced, textvariable=self.settings_path)
        self.settings_entry.pack(side="left", fill="x", expand=True, padx=8)
        self.browse_button = ttk.Button(advanced, text="찾아보기", command=self.browse)
        self.browse_button.pack(side="left")
        self.configuration_widgets.extend(((self.algorithm_box, "readonly"), (self.settings_entry, "normal"), (self.browse_button, "normal")))
        self.excel_fields = ttk.Frame(connection)
        self.excel_fields.grid(row=4, column=0, columnspan=5, sticky="ew", pady=(10, 0))
        for index, (title, variable, width) in enumerate((("시트(빈칸: 첫 시트)", self.workbook_sheet, 15),
                ("온도 시작셀", self.workbook_temperature_cell, 7),
                ("IR 결과 시작셀", self.workbook_result_cell, 7),
                ("현재 IR 입력셀", self.workbook_power_cell, 7),
                ("목표 셀", self.workbook_target_cell, 7),
                ("층 번호 셀(선택)", self.workbook_layer_cell, 7),
                ("이미지 시작셀(선택)", self.workbook_slice_cell, 7))):
            row, group = divmod(index, 3)
            ttk.Label(self.excel_fields, text=title).grid(row=row, column=group * 2, sticky="w", padx=(0, 4), pady=2)
            widget = ttk.Entry(self.excel_fields, textvariable=variable, width=width)
            widget.grid(row=row, column=group * 2 + 1, sticky="w", padx=(0, 20), pady=2)
            self.configuration_widgets.append((widget, "normal"))
        self.excel_fields.grid_remove()

        status_row = ttk.Frame(main)
        status_row.pack(fill="x", pady=(8, 4))
        ttk.Label(status_row, textvariable=self.status, style="Status.TLabel").pack(side="left")
        ttk.Label(main, textvariable=self.capability, wraplength=990, foreground="#9a4c10").pack(fill="x", anchor="w", pady=(0, 6))
        metrics = ttk.Frame(main)
        metrics.pack(fill="x")
        for title, variable in (("최근 완료 층", self.layer), ("T_recoating (CSV)", self.temperature), ("프로그램 상태", self.process_state)):
            card = ttk.LabelFrame(metrics, text=title, padding=8)
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
        self.outputs.pack(fill="x", pady=(8, 4))
        ttk.Label(main, textvariable=self.decision, wraplength=990).pack(fill="x", pady=(0, 6))
        notebook = ttk.Notebook(main)
        notebook.pack(fill="both", expand=True)
        graph_tab = ttk.Frame(notebook, padding=4)
        log_tab = ttk.Frame(notebook, padding=4)
        notebook.add(graph_tab, text="층별 그래프")
        notebook.add(log_tab, text="상태 기록")
        self.charts = LayerCharts(graph_tab, on_retire=self._capture_plot_segment)
        self.charts.set_target_temperature(parse_target_temperature(self.target_temperature.get()))
        self.charts.pack(fill="both", expand=True)
        self.log = scrolledtext.ScrolledText(log_tab, height=7, wrap="word", font=("Segoe UI", 9), state="disabled")
        self.log.pack(fill="both", expand=True, pady=(5, 0))
        self.slice_panel = SlicePanel(notebook,
            busy=lambda: self.closing or self.controller.state == "disconnecting",
            workbook_busy=lambda: self.controller.thread is not None,
            use_workbook=self._use_slice_workbook,
            publish_dataset=self._publish_slice_dataset,
            context_token=lambda: self.controller.generation)
        notebook.add(self.slice_panel, text="슬라이싱 이미지")

    def _publish_slice_dataset(self, dataset, token):
        if (self.closing or token != self.controller.generation or self.controller.thread is None
                or self.controller.state not in ("connecting", "connected")):
            return
        try:
            epoch = self.controller.update_slice_dataset(dataset)
        except ValueError:
            return
        active_epoch, active_dataset = self.controller.slice_dataset_setting.active()
        if epoch == active_epoch:
            self.slice_panel.mark_session_active(active_dataset, epoch)
            return
        self.slice_panel.mark_pending(epoch)
        self._append_log("이미지 교체 예약 · 다음 새 층부터 반영" if dataset is not None
                         else "이미지 선택 해제 예약 · 다음 새 층부터 반영")

    def _use_slice_workbook(self, path):
        if self.controller.thread is not None or self.closing:
            return
        self.algorithm_type.set("Excel 수식 파일")
        self._algorithm_changed()
        self.settings_path.set(path)
        for variable, value in ((self.workbook_sheet, "수동 테스트"), (self.workbook_temperature_cell, "B13"),
                (self.workbook_power_cell, "C13"), (self.workbook_result_cell, "D13"),
                (self.workbook_target_cell, "B5"), (self.workbook_layer_cell, "A13"),
                (self.workbook_slice_cell, "BL13")):
            variable.set(value)
        self._append_log("슬라이싱 수동시트 지정 · 이미지 BL:BW 자동 입력 · 기존 G·알파·베타 수식 유지")

    def browse(self):
        excel = self.algorithm_type.get() == "Excel 수식 파일"
        value = filedialog.askopenfilename(parent=self.root,
            title="Excel 알고리즘 선택" if excel else "알고리즘 설정(선택)",
            filetypes=(("Excel 통합 문서", "*.xlsx"),) if excel else (("JSON 설정", "*.json"),))
        if value:
            self.settings_path.set(value)

    def _algorithm_changed(self, _event=None):
        excel = self.algorithm_type.get() == "Excel 수식 파일"
        self.settings_path.set("")
        self.algorithm_path_label.set("Excel 파일" if excel else "설정 JSON(선택)")
        if excel:
            self.excel_fields.grid()
        else:
            self.excel_fields.grid_remove()

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
            if self.slice_panel.running:
                raise ValueError("슬라이싱 이미지 분석 완료 후 연결하세요.")
            if self.slice_panel.paths and self.slice_panel.dataset is None:
                raise ValueError("선택한 이미지 분석을 완료하거나 선택을 지운 후 연결하세요.")
            workbook = None
            settings = None
            if self.algorithm_type.get() == "Excel 수식 파일":
                workbook = WorkbookSpec(path=self.settings_path.get().strip(), sheet=self.workbook_sheet.get().strip(),
                    temperature_cell=self.workbook_temperature_cell.get().strip(),
                    actual_power_cell=self.workbook_power_cell.get().strip(),
                    recommendation_cell=self.workbook_result_cell.get().strip(),
                    target_cell=self.workbook_target_cell.get().strip(),
                    layer_cell=self.workbook_layer_cell.get().strip() or None,
                    slice_start_cell=self.workbook_slice_cell.get().strip() or None)
                workbook.validate()
            elif self.settings_path.get().strip():
                settings = Path(self.settings_path.get().strip())
            options = SessionOptions(target=target, mode=MODE_LABELS[self.mode.get()],
                target_temperature_c=parse_target_temperature(self.target_temperature.get()),
                settings_path=settings, workbook_spec=workbook, slice_dataset=self.slice_panel.dataset)
            if self.controller.connect(options):
                self.charts.reset()
                self._closing_charts_finalized = False
                self.archive.submit(retry_failed=True)
                self._history_generation = self.controller.generation
                self._history_machine = options.target.machine
                self._history_target_temperature = options.target_temperature_c
                self.charts.set_target_temperature(options.target_temperature_c)
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

    def apply_target_temperature(self):
        try:
            value = parse_target_temperature(self.target_temperature.get())
            self.controller.update_target_temperature(value)
            self._append_log(f"목표 온도 {value:g} °C 예약 · 다음 새 층부터 반영")
        except ValueError as error:
            self._append_log(str(error))

    def _refresh_buttons(self):
        closed = getattr(self.controller, "closed", False)
        busy = self.controller.thread is not None or self.closing or closed
        for widget, idle_state in self.configuration_widgets:
            widget.configure(state="disabled" if busy else idle_state)
        self.slice_panel.set_locked(self.closing or closed or self.controller.state == "disconnecting")
        target_ready = (self.controller.thread is not None and not self.closing
                        and self.controller.state in ("connecting", "connected"))
        self.temperature_entry.configure(state="disabled" if self.closing or closed or self.controller.state == "disconnecting" else "normal")
        self.target_apply_button.configure(state="normal" if target_ready else "disabled")
        self.search_button.configure(state="disabled" if busy or self.searching else "normal")
        self.connect_button.configure(state="disabled" if busy or self.searching or self.slice_panel.running or not self.target_map else "normal")
        self.disconnect_button.configure(state="normal" if busy and not self.closing and not closed and self.controller.state != "disconnecting" else "disabled")

    def _append_log(self, message):
        value = str(message)[:4000]
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S ") + value + "\n")
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 101:
            self.log.delete("1.0", f"{lines - 100}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _reconcile_slice_state(self):
        # Activation events share a bounded display queue and may be dropped.
        # The immutable worker binding is authoritative, even after UI backlog.
        setting = self.controller.slice_dataset_setting
        if setting is None or self.controller.state not in ("connecting", "connected"):
            return
        epoch, dataset = setting.active()
        desired_epoch, _ = setting.desired()
        panel = self.slice_panel
        if panel.active_epoch != epoch or panel.active_dataset is not dataset:
            panel.mark_session_active(dataset, epoch)
        if desired_epoch != epoch:
            if panel.pending_epoch != desired_epoch:
                panel.mark_pending(desired_epoch)
        elif panel.pending_epoch is not None:
            panel.pending_epoch = None
            panel._active_text()

    def _capture_plot_segment(self, segment, points):
        self.archive.capture((self._history_generation, segment), points, self._history_machine,
                             target_temperature=self._history_target_temperature)

    def _capture_current_plot(self):
        self._capture_plot_segment(self.charts.history.segment_id, self.charts.history.points)

    def _record_chart_payload(self, payload):
        if self.charts.record(payload):
            self._history_machine = payload["snapshot"].target.machine
            target = payload.get("display_target_c")
            if isinstance(target, (int, float)) and not isinstance(target, bool) and math.isfinite(target):
                # record() can retire the old run; update its target only AFTER
                # that callback captured the outgoing segment's metadata.
                self._history_target_temperature = target

    def _poll_archives(self):
        for event in self.archive.poll():
            if event["kind"] == "saved":
                self._append_log(f"CSV·플롯 자동 저장 완료 · {event['row_count']}행 · {event['path']}")
                for name, path in event.get("plot_paths", {}).items():
                    self._append_log(f"{'온도' if name == 'temperature' else 'IR'} 플롯 PNG · {path}")
            else:
                message = f"CSV·플롯 저장 미완료 · {event['row_count']}행 메모리에 보존 · {event['error']}"
                completed = [event.get("path")] + list(event.get("plot_paths", {}).values())
                for path in completed:
                    if path:
                        self._append_log(f"저장 완료된 파일 보존 · {path}")
                self._append_log(message)
                write_error_log(message)

    def _show_snapshot(self, payload):
        snapshot = payload["snapshot"]
        if "display_target_c" in payload:
            self.charts.set_target_temperature(payload["display_target_c"])
        self._record_chart_payload(payload)
        features = payload.get('slice_features')
        if features is None:
            self.slice_panel.live.set("실행 중 층 데이터: 이미지 미연결 또는 유효한 관찰 없음")
        else:
            current = (f"중앙 {features['center_coverage_pct']:.3f}% / 전체 {features['whole_coverage_pct']:.3f}%"
                       if features['image_valid'] else '미확인')
            previous = (f"중앙 {features['previous_center_coverage_pct']:.3f}% / 전체 {features['previous_whole_coverage_pct']:.3f}%"
                        if features['previous_image_valid'] else '미확인')
            self.slice_panel.live.set(f"이번 층 계획: {current} · 이전 층 계획: {previous} · 센서 위치/실제 분사 확인값 아님")
        self.layer.set("—" if snapshot.layer_index is None else f"{snapshot.layer_index + 1} 층")
        self.temperature.set("—" if snapshot.t_recoating_c is None else f"{snapshot.t_recoating_c:.1f} °C")
        self.process_state.set(snapshot.machine_state)
        unavailable = not snapshot.control_available
        self.capability.set((("설정 확인 중 · " if snapshot.control_recoverable else "제어 불가 · ") + snapshot.control_unavailable_reason) if unavailable else
            "CSV 자동 적용 중 · Process 표 접근 없음 · 다른 탭/최소화 가능"
            if payload["effective_mode"] == "control" else "기존 Sequences IR + Save 경로 확인됨")
        self.status.set("연결됨 · 제어 불가" if payload["effective_mode"] == "control-unavailable" else
                        "연결됨 · " + {"monitor": "조회", "preview": "계산 미리보기", "control": "UI 자동 적용", "waiting": "자동 상태 대기", "control-waiting": "설정 확인·IR 적용 대기"}[payload["effective_mode"]])
        for heater, value in ((1, snapshot.heater1_setting_percent), (2, snapshot.heater2_setting_percent)):
            proposed = payload["proposed"].get(1 if snapshot.coupled_output else heater)
            self.outputs.item(f"heater{heater}", values=("—" if value is None else value,
                "—" if proposed is None else proposed, "공통 IR 설정" if snapshot.coupled_output else "개별 설정"))
        reasons = " / ".join(dict.fromkeys(payload["reasons"].values()))
        self.decision.set(reasons or (payload["status"] + (" · " + snapshot.details if snapshot.details else "")))

    def _pump(self):
        self._poll_archives()
        if self.closing:
            if not self._closing_archive_started:
                if not self._closing_charts_finalized:
                    for event in self.controller.drain_chart_events():
                        self._record_chart_payload(event.payload)
                    discovering = self.discovery_thread is not None and self.discovery_thread.is_alive()
                    expired = time.monotonic() >= self.close_deadline
                    if (not self.controller.worker_alive and not discovering and not self.controller.chart_events_pending) or expired:
                        if expired:
                            for event in self.controller.drain_chart_events(128):
                                self._record_chart_payload(event.payload)
                        self._capture_current_plot()
                        self._closing_charts_finalized = True
                if self._closing_charts_finalized:
                    self.archive.submit(retry_failed=True)
                    self._closing_archive_started = True
                    self.status.set("CSV·플롯 자동 저장 중…")
            if self._closing_archive_started and not self.archive.busy:
                if self.archive.failed:
                    folder = filedialog.askdirectory(parent=self.root,
                        title="CSV·플롯 저장 미완료 · 다른 폴더 선택 (취소하면 기록과 창 유지)")
                    if folder:
                        self.archive.submit(retry_failed=True, directory=folder)
                    else:
                        self.closing = False
                        self._closing_archive_started = False
                        self.status.set(f"CSV·플롯 저장 미완료 · {self.archive.retained_rows}행 메모리에 보존 중")
                        self._refresh_buttons()
                else:
                    self.archive.close()
                    self.root.destroy()
                    return
            self.root.after(50, self._pump)
            return
        if getattr(self.controller, "closed", False):
            # A canceled failed-export close leaves a read-only recovery window.
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
                setting = self.controller.slice_dataset_setting
                if setting is not None:
                    epoch, dataset = setting.active()
                    self.slice_panel.mark_session_active(dataset, epoch)
            elif event.kind == "slice_activated":
                setting = self.controller.slice_dataset_setting
                if setting is not None and event.payload['epoch'] == setting.desired()[0]:
                    self.slice_panel.mark_session_active(event.payload['dataset'], event.payload['epoch'])
            elif event.kind == "snapshot":
                self._show_snapshot(event.payload)
            elif event.kind == "chart_snapshot":
                self._record_chart_payload(event.payload)
            elif event.kind == "log":
                self._append_log(event.payload)
            elif event.kind == "error":
                self.status.set("연결 오류")
                self.capability.set(str(event.payload))
                self._append_log(event.payload)
            elif event.kind == "finished":
                self._capture_current_plot()
                self.archive.submit()
                self.slice_panel.clear_session()
                self.status.set("연결 오류" if self.controller.error else "연결 해제됨")
                self._append_log("외부 모듈 연결 종료. 원본 프로그램의 가열 ON/OFF는 변경하지 않습니다.")
        self._reconcile_slice_state()
        self._refresh_buttons()
        self.root.after(50, self._pump)

    def close(self):
        if self.closing:
            return
        self.closing = True
        self._closing_archive_started = False
        self.slice_panel.cancel_event.set()
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
