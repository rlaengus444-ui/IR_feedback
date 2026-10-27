"""Offline, cancellable slicing-image analysis panel. UI owns no device I/O."""
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from slice_data import SliceConfig, analyze
from slice_workbook import save_workbook

class SlicePanel(ttk.Frame):
    def __init__(self, parent, busy=lambda:False, use_workbook=lambda path:None):
        super().__init__(parent, padding=8)
        self.busy=busy;self.use_workbook=use_workbook
        self.paths=();self.dataset=None;self.running=False;self.cancel_event=threading.Event()
        self.worker=None;self.pump_id=None
        self.generation=0;self.events=queue.Queue();self.progress=queue.Queue(maxsize=1);self.debounce=None;self.dead=False
        self.radius=tk.StringVar();self.dpi_x=tk.StringVar(value='400');self.dpi_y=tk.StringVar(value='400')
        self.threshold=tk.StringVar(value='0');self.first_layer=tk.StringVar(value='1')
        self.polarity=tk.StringVar(value='흰색');self.numbering=tk.StringVar(value='파일명 숫자')
        self.live=tk.StringVar(value='실행 중 층 데이터: 연결 전')
        self.summary=tk.StringVar(value='중앙 반경 n mm를 입력하고 ZIP 또는 이미지 파일을 선택하세요.')
        self.selected=tk.StringVar(value='선택한 파일 없음')
        self.configuration_widgets=[]
        self._build()
        for variable in (self.radius,self.dpi_x,self.dpi_y,self.threshold,self.first_layer,self.polarity,self.numbering):
            variable.trace_add('write', self._changed)
        self.bind('<Destroy>',self._destroy,add='+')
        self.pump_id=self.after(100,self._pump)

    def _build(self):
        row=ttk.Frame(self);row.pack(fill='x')
        for text,command in [('ZIP 불러오기',self.load_zip),('이미지 전체 선택',self.load_files),('선택 지우기',self.clear)]:
            widget=ttk.Button(row,text=text,command=command);widget.pack(side='left',padx=(0,7));self.configuration_widgets.append((widget,'normal'))
        ttk.Button(row,text='분석 취소',command=self.cancel).pack(side='left')
        ttk.Label(self,textvariable=self.selected,wraplength=1000).pack(fill='x',pady=6)
        settings=ttk.Frame(self);settings.pack(fill='x')
        items=[('중앙 반경 mm',self.radius,9),('X DPI',self.dpi_x,8),('Y DPI',self.dpi_y,8),('첫 파일 대응 층',self.first_layer,8),('분사 기준 0~254',self.threshold,6)]
        for i,(label,var,width) in enumerate(items):
            ttk.Label(settings,text=label).grid(row=0,column=i*2,sticky='w',padx=(0,4))
            widget=ttk.Entry(settings,textvariable=var,width=width);widget.grid(row=0,column=i*2+1,padx=(0,12));self.configuration_widgets.append((widget,'normal'))
        row=ttk.Frame(self);row.pack(fill='x',pady=8)
        for label,var,values in [('분사 색상',self.polarity,('흰색','검정색')),('층 번호 배정',self.numbering,('파일명 숫자','정렬순 연속'))]:
            ttk.Label(row,text=label).pack(side='left')
            widget=ttk.Combobox(row,textvariable=var,values=values,width=15,state='readonly');widget.pack(side='left',padx=(5,16));self.configuration_widgets.append((widget,'readonly'))
        widget=ttk.Button(row,text='다시 분석',command=self.start);widget.pack(side='left');self.configuration_widgets.append((widget,'normal'))
        ttk.Label(self,text='이미지 중앙의 원형 영역을 분석합니다. 센서 실제 위치·도포 실행·건조 완료를 확정하는 데이터가 아닙니다.\n파일명 숫자 배정은 누락 층을 미확인으로 남깁니다. 정렬순 연속은 사용자가 실제 층 순서를 확인한 경우에만 선택하세요.',wraplength=1000,foreground='#836118').pack(fill='x')
        ttk.Label(self,textvariable=self.summary,wraplength=1000).pack(fill='x',pady=7)
        ttk.Label(self,textvariable=self.live,wraplength=1000).pack(fill='x',pady=(0,6))
        self.table=ttk.Treeview(self,columns=('layer','name','center','mean','whole'),show='headings',height=8)
        for key,label,width in [('layer','층',75),('name','이미지',260),('center','중앙 도포율 %',130),('mean','중앙 평균강도 %',130),('whole','전체 도포율 %',130)]:
            self.table.heading(key,text=label);self.table.column(key,width=width)
        self.table.pack(fill='both',expand=True)
        row=ttk.Frame(self);row.pack(fill='x',pady=8)
        ttk.Button(row,text='전체 분석 CSV 저장',command=self.export_csv).pack(side='left',padx=(0,8))
        ttk.Button(row,text='수동 계산시트 저장 / 사용',command=self.export_workbook).pack(side='left')

    def load_zip(self):
        if self.busy():return
        path=filedialog.askopenfilename(parent=self,title='슬라이싱 ZIP 선택',filetypes=[('ZIP','*.zip')])
        if path:self.set_paths((path,))

    def load_files(self):
        if self.busy():return
        paths=filedialog.askopenfilenames(parent=self,title='층 이미지 전체 선택 (Ctrl+A)',filetypes=[('슬라이싱 이미지','*.png *.bmp *.tif *.tiff *.jpg *.jpeg'),('모든 파일','*.*')])
        if paths:self.set_paths(paths)

    def set_paths(self,paths):
        if self.busy():return
        self.paths=tuple(paths)
        self.selected.set((Path(paths[0]).name if len(paths)==1 else f'이미지 {len(paths)}개 선택') if paths else '선택한 파일 없음')
        self._changed()

    def _changed(self,*_):
        self.dataset=None;self.cancel_event.set();self.generation+=1;self.running=False
        self.table.delete(*self.table.get_children())
        self.summary.set('설정 변경됨 · 분석값 없음. 반경/DPI/층 대응을 확인하세요.')
        if self.debounce is not None:self.after_cancel(self.debounce);self.debounce=None
        if self.paths and not self.busy():self.debounce=self.after(450,self.start)

    def read_config(self):
        try:
            return SliceConfig(float(self.radius.get()),float(self.dpi_x.get()),float(self.dpi_y.get()),self.polarity.get()=='흰색',int(self.threshold.get()),int(self.first_layer.get()),'filename' if self.numbering.get()=='파일명 숫자' else 'sequence')
        except ValueError as error:
            raise ValueError('중앙 반경·DPI·층 번호·분사 기준을 확인하세요. '+str(error)) from error

    def start(self):
        self.debounce=None
        if self.busy() or not self.paths:return
        try:config=self.read_config()
        except ValueError as error:self.summary.set(str(error));return
        if self.worker is not None and self.worker.is_alive():
            self.cancel_event.set();self.running=True
            self.debounce=self.after(100,self.start)
            return
        self.cancel_event.set();self.cancel_event=threading.Event();cancel=self.cancel_event
        self.generation+=1;generation=self.generation;paths=self.paths
        self.dataset=None;self.running=True;self.summary.set('분석 중… 운전 중 이미지 디코딩은 수행하지 않습니다.')
        def progress(done,total):
            try:self.progress.get_nowait()
            except queue.Empty:pass
            try:self.progress.put_nowait((generation,done,total))
            except queue.Full:pass
        def work():
            try:value=analyze(paths,config,cancel=cancel,progress=progress);error=None
            except Exception as exc:value=None;error=str(exc)
            self.events.put((generation,value,error))
        self.worker=threading.Thread(target=work,name='IR-slice-analysis',daemon=True)
        self.worker.start()

    def _pump(self):
        if self.dead:return
        try:
            generation,done,total=self.progress.get_nowait()
            if generation==self.generation:self.summary.set(f'슬라이싱 분석 {done}/{total}')
        except queue.Empty:pass
        while True:
            try:generation,value,error=self.events.get_nowait()
            except queue.Empty:break
            if generation!=self.generation:continue
            self.running=False
            if error:self.summary.set('분석 실패: '+error);continue
            self.dataset=value;self.table.delete(*self.table.get_children())
            for r in value.records[:1000]:self.table.insert('', 'end',values=(r.layer,r.name,f'{r.center_coverage_pct:.3f}',f'{r.center_intensity_pct:.3f}',f'{r.whole_coverage_pct:.3f}'))
            first,last=value.records[0],value.records[-1]
            self.summary.set(f'{len(value.records)}개 분석 완료 · {first.layer}~{last.layer}층 · 원형 영역 {first.roi_pixels}픽셀 · SHA256 {value.fingerprint[:16]}'+(' · 표는 처음 1000개, CSV에는 전체 저장' if len(value.records)>1000 else ''))
        self.pump_id=self.after(100,self._pump)

    def cancel(self):
        if self.busy():return
        if self.debounce is not None:self.after_cancel(self.debounce);self.debounce=None
        self.cancel_event.set();self.generation+=1;self.running=False;self.dataset=None;self.summary.set('분석 취소됨 · 적용할 이미지 데이터 없음')

    def clear(self):
        if self.busy():return
        self.cancel();self.paths=();self.selected.set('선택한 파일 없음');self.table.delete(*self.table.get_children())
        if self.debounce is not None:self.after_cancel(self.debounce);self.debounce=None

    def export_csv(self):
        if self.dataset is None:messagebox.showinfo('슬라이싱 분석','분석을 먼저 완료하세요.',parent=self);return
        path=filedialog.asksaveasfilename(parent=self,defaultextension='.csv',initialfile='슬라이싱_중앙영역_분석.csv',filetypes=[('CSV','*.csv')])
        if path:
            try:self.dataset.export_csv(path)
            except OSError as error:messagebox.showerror('저장 실패',str(error),parent=self)

    def export_workbook(self):
        if self.busy() or self.running:return
        if self.paths and self.dataset is None:messagebox.showinfo('수동 계산시트','분석을 완료하거나 선택을 지우세요.',parent=self);return
        path=filedialog.asksaveasfilename(parent=self,defaultextension='.xlsx',initialfile='IR_슬라이싱_수동계산.xlsx',filetypes=[('Excel','*.xlsx')])
        if path:
            try:save_workbook(path,self.dataset)
            except Exception as error:messagebox.showerror('시트 저장 실패',str(error),parent=self);return
            self.use_workbook(path)
            self.summary.set('수동 계산시트 저장 완료 · Excel 입력 셀을 함께 설정했습니다. 이미지 기반 급락 억제와 기존 G·알파·베타 계산식, 억제 설정을 편집할 수 있습니다.')

    def _destroy(self,event):
        if event.widget is self:
            self.dead=True;self.cancel_event.set()
            if self.pump_id is not None:self.after_cancel(self.pump_id);self.pump_id=None
            if self.debounce is not None:
                self.after_cancel(self.debounce);self.debounce=None
