"""Asynchronous, loss-aware archives of complete chart histories."""
from datetime import datetime
import os
from pathlib import Path
import queue
import re
import sys
import tempfile
import threading

from layer_charts import _write_csv
from plot_images import write_plot


def _default_directories():
    executable_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    local = os.environ.get("LOCALAPPDATA")
    user_dir = Path(local) if local else Path.home() / ".local" / "share"
    return (executable_dir / "records", user_dir / "IRFeedback" / "records")


class PlotArchive:
    """Owned by the GUI thread; file operations happen on one daemon worker."""
    def __init__(self, directories=None, writer=_write_csv, thread_factory=threading.Thread, image_writer=write_plot):
        self.directories = tuple(Path(p) for p in (directories if directories is not None else _default_directories()))
        self.writer = writer
        self.thread_factory = thread_factory
        self.image_writer = image_writer
        self._records = {}
        self._work = queue.Queue()
        self._results = queue.Queue()
        self._thread = None
        self._closed = False

    def capture(self, key, points, machine="M1", target_temperature=None):
        if self._closed:
            raise RuntimeError("archive is closed")
        key = tuple(key)
        if key in self._records:
            return False
        points = tuple(points)
        if not points:
            return False
        self._records[key] = {"points": points, "machine": str(machine), "state": "captured",
                              "target_temperature": target_temperature, "path": None,
                              "plot_paths": {"temperature": None, "ir": None}}
        return True

    def submit(self, retry_failed=False, directory=None):
        if self._closed:
            raise RuntimeError("archive is closed")
        eligible = [(key, record) for key, record in self._records.items()
                    if record["state"] == "captured" or (retry_failed and record["state"] == "failed")]
        if not eligible:
            return
        if self._thread is None:
            try:
                self._thread = self.thread_factory(target=self._run, daemon=True)
                self._thread.start()
            except Exception as error:
                self._thread = None
                for key, record in eligible:
                    record["state"] = "failed"
                    self._results.put({"kind": "error", "key": key, "path": record["path"],
                                       "row_count": len(record["points"]), "error": str(error),
                                       "plot_paths": dict(record["plot_paths"])})
                return
        destinations = (Path(directory),) if directory is not None else self.directories
        for key, record in eligible:
            record["state"] = "queued"
            self._work.put((key, record["points"], record["machine"], destinations,
                            record["target_temperature"], record["path"], dict(record["plot_paths"])))

    def _run(self):
        while True:
            item = self._work.get()
            if item is None:
                return
            key, points, machine, directories, target, csv_path, plots = item
            errors = []
            safe_machine = re.sub(r"[^A-Za-z0-9_-]", "_", machine)[:40] or "machine"
            safe_key = "-".join(re.sub(r"[^A-Za-z0-9_-]", "_", str(part))[:40] for part in key)
            prefix = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + f"_{safe_machine}_{safe_key}_"
            if csv_path is None:
                for directory in directories:
                    reserved = None
                    try:
                        directory.mkdir(parents=True, exist_ok=True)
                        fd, reserved = tempfile.mkstemp(prefix=prefix, suffix=".csv", dir=directory)
                        os.close(fd)
                        self.writer(Path(reserved), points)
                        csv_path = reserved
                        break
                    except Exception as error:
                        errors.append(f"CSV: {error}")
                        if reserved is not None:
                            try:
                                os.unlink(reserved)
                            except OSError:
                                pass
            if csv_path is not None:
                for kind in ("temperature", "ir"):
                    if plots[kind] is not None:
                        continue
                    for directory in directories:
                        reserved = temporary = None
                        try:
                            directory.mkdir(parents=True, exist_ok=True)
                            stem = Path(csv_path).stem
                            desired = directory / f"{stem}_{kind}.png"
                            try:
                                fd = os.open(desired, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                                reserved = str(desired)
                            except FileExistsError:
                                fd, reserved = tempfile.mkstemp(prefix=f"{stem}_{kind}_", suffix=".png", dir=directory)
                            os.close(fd)
                            fd, temporary = tempfile.mkstemp(prefix=".ir-plot-", suffix=".tmp", dir=directory)
                            os.close(fd)
                            self.image_writer(Path(temporary), points, kind, target)
                            os.replace(temporary, reserved)
                            temporary = None
                            plots[kind] = reserved
                            break
                        except Exception as error:
                            errors.append(f"{kind} PNG: {error}")
                            for path in (temporary, reserved):
                                if path is not None:
                                    try:
                                        os.unlink(path)
                                    except OSError:
                                        pass
            complete = csv_path is not None and all(plots.values())
            self._results.put({"kind": "saved" if complete else "error", "key": key,
                               "path": csv_path, "plot_paths": plots, "row_count": len(points),
                               "error": None if complete else "; ".join(errors) or "No archive directory configured"})
            del item, points

    def poll(self):
        events = []
        while True:
            try:
                event = self._results.get_nowait()
            except queue.Empty:
                break
            record = self._records[event["key"]]
            record["path"] = event["path"]
            record["plot_paths"] = dict(event["plot_paths"])
            if event["kind"] == "saved":
                record["state"] = "saved"
                record["points"] = ()
            else:
                record["state"] = "failed"
            events.append(event)
        return events

    @property
    def busy(self):
        return any(record["state"] == "queued" for record in self._records.values())

    @property
    def failed(self):
        return any(record["state"] == "failed" for record in self._records.values())

    @property
    def retained_rows(self):
        return sum(len(record["points"]) for record in self._records.values())

    def close(self):
        if not self._closed:
            self._closed = True
            if self._thread is not None:
                self._work.put(None)
