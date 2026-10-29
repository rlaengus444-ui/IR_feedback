"""Independent UI-path watcher and complete, cancellable image-set publication."""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import hashlib
import os
from pathlib import Path
import queue
import threading
import time

from csv_source import shared_binary
from slice_data import EXTENSIONS, MAX_FILES, MAX_IMAGE_BYTES, SliceConfig, _mapping, analyze
from windows_slice_source import BindingLost, SourceReadError, WindowsPathReader, canonical, same_source_path


@dataclass(frozen=True)
class SourceFile:
    path: str
    size: int
    mtime_ns: int
    inode: int


def inventory(directory, cancel=None):
    """Flat folder only; no recursive discovery, image decoding or hashing."""
    result = []
    with os.scandir(directory) as entries:
        for index, entry in enumerate(entries):
            if index > MAX_FILES * 2:
                raise ValueError('자동 이미지 폴더의 항목 수가 너무 많습니다.')
            if cancel is not None and cancel.is_set():
                raise InterruptedError()
            if Path(entry.name).suffix.lower() not in EXTENSIONS:
                continue
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                raise ValueError('자동 입력에는 일반 이미지 파일만 사용하세요.')
            stat = entry.stat(follow_symlinks=False)
            if not 0 < stat.st_size <= MAX_IMAGE_BYTES:
                raise ValueError('자동 이미지 파일의 크기를 확인하세요.')
            result.append(SourceFile(os.path.abspath(entry.path), stat.st_size,
                                     stat.st_mtime_ns, stat.st_ino))
            if len(result) > MAX_FILES:
                raise ValueError('자동 이미지 수가 100000개를 초과했습니다.')
    if not result:
        raise ValueError('지정한 폴더에 슬라이싱 이미지가 없습니다.')
    return tuple(sorted(result, key=lambda value: canonical(value.path)))


def content_digest(path, cancel=None, budget=1.0):
    deadline = time.monotonic() + budget
    digest = hashlib.sha256(); size = 0
    with shared_binary(Path(path)) as stream:
        while True:
            if cancel is not None and cancel.is_set():
                raise InterruptedError()
            if time.monotonic() > deadline:
                raise TimeoutError('이미지 내용 확인 시간 초과')
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_IMAGE_BYTES:
                raise ValueError('이미지 내용 확인 크기 초과')
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class AutoAdmission:
    version: int
    epoch: int
    features: dict | None
    guard: dict | None


class AutoSliceSource:
    """Two workers: bounded path/metadata polling and image analysis/digests.

    Neither worker touches Tk or uses the control adapter. The gate is invalidated
    before analysis is queued. CSV is never a discovery/change trigger.
    """
    def __init__(self, target, config, binding=None, *, reader=None, analyzer=analyze,
                 clock=datetime.now, monotonic=time.monotonic, poll_seconds=.5,
                 stable_seconds=.5, scan_seconds=1.0):
        if config is not None and (not isinstance(config,SliceConfig) or config.filename_origin is None):
            raise ValueError('자동 입력의 파일명 기준 번호를 지정하세요.')
        if binding is not None and binding.target != target:
            raise ValueError('같은 BJT 창의 경로 표시 칸을 지정하세요.')
        self.target = target; self.reader = reader or WindowsPathReader()
        self.config = config; self.binding = binding; self.analyzer = analyzer
        self.clock = clock; self.monotonic = monotonic
        self._wall_anchor = clock();self._mono_anchor = monotonic()
        self._wall_floor = self._wall_anchor
        self.poll_seconds = poll_seconds; self.stable_seconds = stable_seconds
        self.scan_seconds = scan_seconds
        self._lock = threading.Lock(); self._stop = threading.Event()
        self._wake = threading.Event(); self._cancel = threading.Event()
        self._jobs = queue.Queue(maxsize=1); self._threads = []; self._setting = None
        self._version = 0; self._ready = False; self._epoch = None
        self._dataset = None; self._source = None; self._manifest = None
        self._source_paths = {}
        self._ack_directory = None; self._ack_hashes = {}; self._candidate_hashes = {}
        self._directory = None; self._last_directory = None; self._last_manifest = None
        self._stable_at = 0.; self._last_scan = -float('inf'); self._heartbeat = -float('inf')
        self._queued_version = None; self._retry_at = 0.; self._digest_index = 0
        self._needs_reload = False; self._known_powder = {}
        self._observed_context = None; self._observed_rows = {}; self._rejected_layers = set()
        self._proof_cache_key = None; self._frame_ticket = 0
        self._analysis_paused = False
        self._status = '첫 탭 경로 확인 대기'; self._raw = ''
        self._cutoff = self._new_cutoff()

    def _new_cutoff(self):
        # CSVs may have only whole seconds. Never admit the ambiguous second.
        actual=self.clock();tick=self.monotonic()
        predicted=self._wall_anchor+timedelta(seconds=max(0,tick-self._mono_anchor))
        safe=max(actual,predicted,self._wall_floor)
        self._wall_floor=safe
        if actual>predicted:
            self._wall_anchor=actual;self._mono_anchor=tick
        return safe.replace(microsecond=0)+timedelta(seconds=1)

    def _invalidate_locked(self, reason, *, reload=False):
        self._cancel.set(); self._cancel = threading.Event()
        self._version += 1; self._ready = False; self._epoch = None
        self._queued_version = None; self._known_powder.clear()
        self._observed_context = None; self._observed_rows.clear(); self._rejected_layers.clear()
        self._proof_cache_key = None; self._frame_ticket += 1
        self._cutoff = self._new_cutoff(); self._status = reason
        self._needs_reload = self._needs_reload or reload
        self._stable_at = self.monotonic(); self._retry_at = 0.

    def start(self, setting):
        with self._lock:
            if self._setting is not None:
                raise ValueError('자동 이미지 작업은 연결별로 새로 생성해야 합니다.')
            self._setting = setting
        try:
            for name, function in [('IR-auto-path-watch', self._watch), ('IR-auto-slice-analysis', self._work)]:
                thread = threading.Thread(target=function, name=name, daemon=True)
                self._threads.append(thread); thread.start()
        except Exception:
            self.stop(); raise

    def stop(self):
        with self._lock:
            self._stop.set(); self._cancel.set(); self._ready = False
            self._version += 1
        self._wake.set()
        for thread in self._threads:
            if thread is not threading.current_thread() and thread.is_alive():
                thread.join(.05)

    def configure(self, config):
        if config is not None and (not isinstance(config, SliceConfig) or config.filename_origin is None):
            raise ValueError('자동 이미지에는 고정 파일명 기준 번호가 필요합니다.')
        with self._lock:
            if config == self.config:
                return
            self.config = config
            self._analysis_paused = False
            self._invalidate_locked('중앙 반경·DPI·층 대응 변경 · 자동 분석 대기')
        self._wake.set()

    def rebind(self, binding):
        if binding.target != self.target:
            raise ValueError('현재 연결과 같은 BJT 창의 경로를 지정하세요.')
        with self._lock:
            self.binding = binding; self._source = None; self._directory = None
            self._analysis_paused = False
            self._manifest = None; self._heartbeat = -float('inf')
            self._invalidate_locked('경로 표시 칸 변경 · 새 이미지 확인 대기')
        self._wake.set()

    def pause_binding(self):
        with self._lock:
            self.binding = None
            self._invalidate_locked('경로 표시 칸 다시 지정 중 · IR 적용 대기')

    def refresh(self):
        with self._lock:
            self._analysis_paused = False
            self._invalidate_locked('자동 이미지 재분석 요청')
        self._wake.set()

    def cancel_analysis(self):
        with self._lock:
            self._analysis_paused = True
            self._invalidate_locked('자동 분석 취소됨 · 다시 분석을 누르면 재개합니다.')

    def confirm_reload(self):
        with self._lock:
            if not self._needs_reload or self._dataset is None or self._epoch is None:
                raise ValueError('새 이미지 분석 완료 후 재불러오기를 확인하세요.')
            self._needs_reload = False; self._cutoff = self._new_cutoff()
            self._ack_directory = self._directory; self._ack_hashes = dict(self._candidate_hashes)
            self._known_powder.clear(); self._ready = True
            self._observed_context = None; self._observed_rows.clear(); self._rejected_layers.clear()
            self._proof_cache_key = None;self._frame_ticket += 1
            self._status = '재불러오기 확인 · 이후 리코팅 측정과 새 층 대기'

    def guard_failed(self, reason):
        with self._lock:
            self._invalidate_locked('이미지 확인 변경 · ' + str(reason), reload=True)
        self._wake.set()

    def view(self):
        with self._lock:
            return dict(version=self._version, status=self._status, path=self._directory or '',
                        raw=self._raw, dataset=self._dataset, epoch=self._epoch,
                        ready=self._ready, needs_reload=self._needs_reload,
                        bound=self.binding is not None)

    def poll_once(self):
        """One watch tick, also used by deterministic offline race tests."""
        with self._lock:
            binding, config = self.binding, self.config
        if binding is None:
            return
        try:
            source = self.reader.read(binding)
            now = self.monotonic(); directory = canonical(source.directory)
            with self._lock:
                if self._stop.is_set() or binding != self.binding or config != self.config:
                    return
                if self._ready and now - self._heartbeat > 2:
                    self._invalidate_locked('경로 감시 응답 지연 · 최신 이미지 다시 확인')
                    self._directory = None; self._manifest = None
                self._heartbeat = now; self._raw = source.raw_text
                if self._directory != directory:
                    different = self._last_directory is not None and directory != self._last_directory
                    self._invalidate_locked('첫 탭 경로 변경 · 전체 이미지 확인 중')
                    if different:
                        self._needs_reload = False
                    self._directory = directory; self._source = source; self._manifest = None
                    self._last_scan = -float('inf')
                    self._analysis_paused = False
                else:
                    self._source = source
                if config is None:
                    self._status = '중앙 반경·DPI·고정 파일명 기준 번호를 입력하세요.'
                    return
                if now - self._last_scan < self.scan_seconds:
                    return
                version = self._version; self._last_scan = now
                cancel = self._cancel
            manifest = inventory(source.directory, cancel)
            with self._lock:
                if version != self._version or binding != self.binding or self._stop.is_set():
                    return
                if self._manifest != manifest:
                    same_path_changed = (self._last_directory == directory and self._last_manifest is not None
                                         and self._last_manifest != manifest)
                    if self._manifest is not None or same_path_changed:
                        self._invalidate_locked('폴더 이미지 변경 · 다시 분석 중', reload=same_path_changed)
                    self._manifest = manifest; self._stable_at = now
                    self._last_manifest = manifest; self._last_directory = directory
                if (now - self._stable_at >= self.stable_seconds and not self._ready
                        and not self._analysis_paused and self._queued_version != self._version and now >= self._retry_at):
                    self._queued_version = self._version
                    work = (self._version, source, manifest, config, self._cancel)
                    try: self._jobs.get_nowait()
                    except queue.Empty: pass
                    self._jobs.put_nowait(work)
                    self._status = f'자동 이미지 {len(manifest)}개 분석 대기'
        except InterruptedError:
            pass
        except Exception as error:
            with self._lock:
                message = '첫 탭 경로 확인 대기 · ' + str(error)
                if self._status != message or self._ready:
                    self._invalidate_locked(message)
                self._directory = None; self._manifest = None
                if isinstance(error, BindingLost):
                    self.binding = None

    def _watch(self):
        while not self._stop.is_set():
            self.poll_once()
            self._wake.wait(self.poll_seconds); self._wake.clear()

    def analyze_job(self, work):
        version, source, manifest, config, cancel = work
        try:
            paths = tuple(item.path for item in manifest)
            def progress(done, total):
                with self._lock:
                    if version == self._version and not self._stop.is_set():
                        self._status = f'자동 슬라이싱 분석 {done}/{total}'
            value = self.analyzer(paths, config, cancel=cancel, progress=progress)
            originals = dict(_mapping(paths, config))
            after = inventory(source.directory, cancel)
            current = self.reader.read(source.binding)
            with self._lock:
                if self._stop.is_set() or version != self._version or cancel.is_set():
                    return
                if (after != manifest or current.binding != self.binding
                        or canonical(current.directory) != self._directory):
                    self._invalidate_locked('분석 중 이미지셋 변경 · 최신 경로 재확인')
                    self._manifest = None
                    return
                # Lock order is gate -> setting; session never holds both locks.
                hashes = {canonical(originals[r.layer]):r.sha256 for r in value.records}
                if self._ack_directory == self._directory and hashes != self._ack_hashes:
                    self._needs_reload = True
                epoch = self._setting.request(value, force=True)
                self._dataset = value; self._epoch = epoch
                self._source_paths = originals
                self._candidate_hashes = hashes
                if not self._needs_reload:
                    self._ack_directory = self._directory; self._ack_hashes = dict(hashes)
                self._ready = not self._needs_reload
                self._status = (f'{len(value.records)}개 자동 분석 완료 · 같은 경로 재불러오기 확인 필요'
                    if self._needs_reload else f'{len(value.records)}개 자동 분석 완료 · 새 측정 대기')
        except InterruptedError:
            pass
        except Exception as error:
            with self._lock:
                if version == self._version and not self._stop.is_set():
                    self._status = '자동 분석 대기 · ' + str(error)
                    self._queued_version = None; self._retry_at = self.monotonic() + 3

    def _work(self):
        while not self._stop.is_set():
            try:
                work = self._jobs.get(timeout=.25)
            except queue.Empty:
                self.background_digest_once()
            else:
                self.analyze_job(work)

    def background_digest_once(self):
        with self._lock:
            if self._dataset is None or self._epoch is None or self._stop.is_set():
                return
            version = self._version; cancel = self._cancel
            record = self._dataset.records[self._digest_index % len(self._dataset.records)]
            path = self._source_paths[record.layer]
            self._digest_index += 1
        try:
            digest = content_digest(path, cancel, budget=.25)
            if digest != record.sha256:
                with self._lock:
                    if version == self._version and not self._stop.is_set():
                        self._invalidate_locked('동일 경로 이미지 내용 변경 · 재분석 중', reload=True)
        except (InterruptedError, TimeoutError):
            pass  # Admission still requires bounded, synchronous content proofs.
        except Exception as error:
            with self._lock:
                if version == self._version:
                    self._invalidate_locked('이미지 읽기 대기 · ' + str(error))

    def _candidate_frame(self, snapshot, epoch, dataset):
        with self._lock:
            if self._ready and self.monotonic() - self._heartbeat > 2:
                self._invalidate_locked('경로 감시 응답 지연 · 최신 이미지 다시 확인')
                self._directory = None; self._manifest = None
            if (not self._ready or self._stop.is_set() or epoch != self._epoch
                    or dataset is not self._dataset or self.monotonic() - self._heartbeat > 2):
                return None, self._status or '첫 탭 경로 확인 대기'
            stamp = snapshot.recoating_finished
            if (snapshot.observation_key is None or not isinstance(snapshot.layer_index,int)
                    or isinstance(snapshot.layer_index,bool)):
                # Normal CSV polling has no fresh key between layers. This is
                # not a source change and must not reset Excel every 5 seconds.
                return AutoAdmission(self._version,epoch,None,None), ''
            context = (snapshot.run_key,snapshot.csv_generation)
            if self._observed_context != context:
                changed = self._observed_context is not None
                self._known_powder.clear();self._observed_rows.clear();self._rejected_layers.clear()
                self._proof_cache_key = None;self._frame_ticket += 1
                self._observed_context = context
                if changed:
                    return None, '작업/CSV 구간 변경 · 새로운 측정 기준 확인 대기'
            layer = snapshot.layer_index + 1 if isinstance(snapshot.layer_index, int) else None
            observed = (snapshot.observation_key,snapshot.slice_lineage,
                        canonical(snapshot.slicing_image_path),stamp)
            if layer in self._observed_rows and self._observed_rows[layer] != observed:
                self._known_powder.clear();self._rejected_layers.add(layer)
            if layer in self._rejected_layers:
                return None, '동일 층 기록 수정 · 다른 새 층의 이미지·측정 대기'
            if not isinstance(stamp, datetime) or stamp.tzinfo is not None or stamp < self._cutoff:
                return None, '이미지 교체 이후 리코팅 측정 대기 · 지연된 CSV는 기록만 유지'
            record = dataset._by_layer.get(layer)
            if record is None or not snapshot.slice_lineage or not snapshot.slicing_image_path:
                return None, '현재 이미지셋과 CSV 층·이미지 경로 일치 대기'
            features = dataset.features(layer)
            if layer - 1 not in self._known_powder:
                features.update(previous_image_valid=0, previous_center_coverage_pct=None,
                    previous_center_intensity_pct=None, previous_whole_coverage_pct=None,
                    previous_powder_layers=0)
            else:
                features['previous_powder_layers'] = self._known_powder[layer - 1]
            relevant = [record]
            previous = dataset._by_layer.get(layer - 1)
            if previous is not None and features['previous_image_valid']:
                relevant.append(previous)
            proof = self._source.binding.token(self._source.directory)
            proof['files'] = [{'path': self._source_paths[r.layer], 'sha256': r.sha256} for r in relevant]
            proof['lineage'] = snapshot.slice_lineage
            return AutoAdmission(self._version, epoch, features, proof), ''

    def frame(self, snapshot, epoch, dataset, *, verify_lineage=None):
        candidate,reason = self._candidate_frame(snapshot,epoch,dataset)
        if candidate is None or candidate.guard is None:
            return candidate,reason
        key = (candidate.version,epoch,snapshot.run_key,snapshot.csv_generation,
               snapshot.observation_key,snapshot.slice_lineage,
               canonical(snapshot.slicing_image_path),snapshot.recoating_finished)
        with self._lock:
            if candidate.version != self._version or not self._ready:
                return None,'이미지 확인 중 경로 변경'
            if key == self._proof_cache_key:
                return candidate,''  # Already-proven observation; no new learning.
            self._frame_ticket += 1;ticket=self._frame_ticket
            binding=self.binding;cancel=self._cancel
        try:
            if not same_source_path(snapshot.slicing_image_path,candidate.guard['files'][0]['path']):
                return None,'현재 이미지셋과 CSV 층·전체 이미지 경로 일치 대기'
            # Before any Excel row/baseline/powder-history promotion, including
            # recommendations that will HOLD and never invoke native apply.
            current=self.reader.read(binding)
            if canonical(current.directory) != candidate.guard['directory']:
                raise SourceReadError('계산 전 이미지 경로가 변경되었습니다.')
            deadline=time.monotonic()+.75
            for file in candidate.guard['files']:
                if content_digest(file['path'],cancel,budget=max(0,deadline-time.monotonic())) != file['sha256']:
                    raise SourceReadError('계산 전 이미지 내용이 변경되었습니다.')
            current=self.reader.read(binding)
            if canonical(current.directory) != candidate.guard['directory']:
                raise SourceReadError('내용 확인 중 이미지 경로가 변경되었습니다.')
            if verify_lineage is not None and not verify_lineage():
                return None,'CSV 행 갱신 중 · 최신 이미지·측정 대응 대기'
        except InterruptedError:
            return None,'이미지 확인 중 변경/취소'
        except (SourceReadError,OSError,TimeoutError,ValueError) as error:
            with self._lock:
                if candidate.version == self._version and not self._stop.is_set():
                    self._invalidate_locked('계산 전 이미지 확인 대기 · '+str(error),reload=True)
            return None,str(error)
        with self._lock:
            if (ticket != self._frame_ticket or candidate.version != self._version
                    or not self._ready or self._stop.is_set() or epoch != self._epoch
                    or self.monotonic()-self._heartbeat>2):
                return None,'이미지 확인 중 새 경로/설정 도착 · 이전 결과 보류'
            layer=snapshot.layer_index+1
            self._observed_rows[layer]=(snapshot.observation_key,snapshot.slice_lineage,
                                        canonical(snapshot.slicing_image_path),snapshot.recoating_finished)
            record=dataset._by_layer[layer]
            self._known_powder[layer]=(self._known_powder.get(layer-1,0)+1
                                      if record.whole_coverage_pct==0 else 0)
            self._proof_cache_key=key
            return candidate,''

    def admit(self, admission):
        with self._lock:
            if self._ready and self.monotonic() - self._heartbeat > 2:
                self._invalidate_locked('경로 감시 응답 지연 · 최신 이미지 다시 확인')
                self._directory = None;self._manifest = None
            return (self._ready and not self._stop.is_set() and admission is not None
                    and admission.version == self._version and admission.epoch == self._epoch
                    and self.monotonic() - self._heartbeat <= 2)
