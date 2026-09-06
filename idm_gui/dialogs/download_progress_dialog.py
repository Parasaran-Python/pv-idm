"""
Iconic IDM Active Download Progress Dialog with Segment Visualizer, Speed Graph & File/Folder Launchers
"""

import os
from typing import Any, Dict, List, Optional
from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from idm_gui.widgets.download_table import format_bytes, format_speed, format_time
from idm_gui.widgets.segment_visualizer import SegmentVisualizerWidget
from idm_gui.widgets.speed_graph import SpeedGraphWidget


class DownloadProgressDialog(QDialog):
    pause_requested = pyqtSignal(str)
    resume_requested = pyqtSignal(str)
    cancel_requested = pyqtSignal(str)
    speed_limit_changed = pyqtSignal(str, int)

    def __init__(self, download_id: str, filename: str = "", save_path: str = "", parent=None):
        super().__init__(parent)
        self.download_id = download_id
        self.filename = filename or "Download"
        self.save_path = save_path or ""
        self.status = "downloading"
        self.total_bytes = 0
        self.downloaded_bytes = 0

        self.setWindowTitle(f"{self.filename} - IDM Download Progress")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setMinimumWidth(580)
        self.setMinimumHeight(440)

        self._setup_ui()

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(10)

        # 1. Top Meta Info
        info_form = QFormLayout()
        info_form.setSpacing(4)
        info_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.status_label = QLabel("Connecting...")
        self.status_label.setStyleSheet("font-weight: bold; color: #48bb78;")
        info_form.addRow("Status:", self.status_label)

        self.size_label = QLabel("0 B / 0 B")
        info_form.addRow("File size:", self.size_label)

        self.speed_label = QLabel("0 KB/s")
        info_form.addRow("Transfer rate:", self.speed_label)

        self.eta_label = QLabel("--:--:--")
        info_form.addRow("Time left:", self.eta_label)

        self.resume_label = QLabel("Yes")
        info_form.addRow("Resume capability:", self.resume_label)

        main_layout.addLayout(info_form)

        # 2. Overall Progress Bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(22)
        main_layout.addWidget(self.progress_bar)

        # 3. Dynamic Segment Visualizer
        seg_box = QGroupBox("Dynamic Multi-Connection Segments")
        seg_layout = QVBoxLayout(seg_box)
        seg_layout.setContentsMargins(6, 6, 6, 6)
        self.segment_visualizer = SegmentVisualizerWidget()
        seg_layout.addWidget(self.segment_visualizer)
        main_layout.addWidget(seg_box)

        # 4. Tabbed Area (Speed Graph & Log)
        self.tabs = QTabWidget()

        # Speed Graph Tab
        self.speed_graph = SpeedGraphWidget()
        self.tabs.addTab(self.speed_graph, "📈 Speed Graph")

        # Log Tab
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.tabs.addTab(self.log_text, "📝 Log")

        main_layout.addWidget(self.tabs)

        # 5. Speed Limiter Slider Control
        limiter_layout = QHBoxLayout()
        self.limit_check = QCheckBox("Speed Limiter:")
        self.limit_slider = QSlider(Qt.Orientation.Horizontal)
        self.limit_slider.setRange(50, 10000)  # 50 KB/s to 10 MB/s
        self.limit_slider.setValue(1000)
        self.limit_slider.setEnabled(False)
        self.limit_val_label = QLabel("1.00 MB/s")
        self.limit_val_label.setEnabled(False)

        self.limit_check.toggled.connect(self._on_limit_toggled)
        self.limit_slider.valueChanged.connect(self._on_limit_changed)

        limiter_layout.addWidget(self.limit_check)
        limiter_layout.addWidget(self.limit_slider)
        limiter_layout.addWidget(self.limit_val_label)
        main_layout.addLayout(limiter_layout)

        # 6. Action Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        # In-Progress Buttons
        self.btn_pause = QPushButton("Pause")
        self.btn_pause.clicked.connect(self._on_pause_toggle)

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self._on_cancel)

        self.btn_hide = QPushButton("Hide")
        self.btn_hide.clicked.connect(self.hide)

        # Completion Action Buttons
        self.btn_open = QPushButton("Open")
        self.btn_open.setStyleSheet("background-color: #2b6cb0; color: white; font-weight: bold; padding: 6px 14px;")
        self.btn_open.clicked.connect(self._on_open_file)
        self.btn_open.hide()

        self.btn_open_folder = QPushButton("📂 Open Folder")
        self.btn_open_folder.setStyleSheet("font-weight: bold; padding: 6px 14px;")
        self.btn_open_folder.clicked.connect(self._on_open_folder)
        self.btn_open_folder.hide()

        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.accept)
        self.btn_close.hide()

        btn_layout.addWidget(self.btn_open)
        btn_layout.addWidget(self.btn_open_folder)
        btn_layout.addWidget(self.btn_close)
        btn_layout.addWidget(self.btn_pause)
        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addWidget(self.btn_hide)

        main_layout.addLayout(btn_layout)

    def update_progress(self, stats: Dict[str, Any]):
        """Update live telemetry statistics."""
        self.status = stats.get("status", "downloading")
        self.status_label.setText(self.status.capitalize())

        if stats.get("save_path"):
            self.save_path = stats.get("save_path")

        dl_bytes = stats.get("downloaded_bytes", 0)
        tot_bytes = stats.get("total_bytes", 0)
        speed = stats.get("speed", 0)
        eta = stats.get("eta", 0)
        resumable = stats.get("resumable", True)

        self.total_bytes = tot_bytes
        self.downloaded_bytes = dl_bytes

        self.size_label.setText(f"{format_bytes(dl_bytes)} / {format_bytes(tot_bytes) if tot_bytes > 0 else 'Unknown'}")
        self.speed_label.setText(format_speed(speed))
        self.eta_label.setText(format_time(eta))
        self.resume_label.setText("Yes" if resumable else "No")

        pct = int(stats.get("progress_pct", 0)) if "progress_pct" in stats else 0
        if tot_bytes > 0:
            pct = int((dl_bytes / tot_bytes) * 100)
            self.progress_bar.setValue(pct)
            self.progress_bar.setFormat(f"{pct}% ({format_bytes(dl_bytes)} of {format_bytes(tot_bytes)})")
        elif pct > 0:
            self.progress_bar.setValue(pct)
            self.progress_bar.setFormat(f"{pct}% ({format_bytes(dl_bytes)})")
        else:
            self.progress_bar.setValue(100 if self.status == "completed" else 0)
            if self.status == "completed":
                self.progress_bar.setFormat(f"100% ({format_bytes(dl_bytes)})")
            elif dl_bytes > 0:
                self.progress_bar.setFormat(f"{format_bytes(dl_bytes)} ({self.status.capitalize()})")
            else:
                self.progress_bar.setFormat(f"0% ({self.status.capitalize()})")

        self.speed_graph.add_speed_sample(speed)

        raw_status = str(stats.get("status", "downloading")).strip().lower()
        self.status = raw_status

        if self.status in ["completed", "finished"]:
            self.status_label.setStyleSheet("font-weight: bold; color: #4299e1;")
            self.status_label.setText("Download Complete")

            # Switch to completion buttons
            self.btn_pause.hide()
            self.btn_cancel.hide()
            self.btn_hide.hide()
            self.btn_open.show()
            self.btn_open_folder.show()
            self.btn_close.show()

            self.limit_check.setEnabled(False)
            self.limit_slider.setEnabled(False)

        elif self.status in ["assembling", "merging"]:
            self.btn_pause.hide()
            self.btn_cancel.show()
            self.btn_hide.show()
            self.btn_open.hide()
            self.btn_open_folder.hide()
            self.btn_close.hide()
            self.status_label.setStyleSheet("font-weight: bold; color: #805ad5;")
            self.status_label.setText("Merging audio & video...")
            self.speed_label.setText("Processing...")
            self.eta_label.setText("Finalizing...")
            self.progress_bar.setValue(100)
            self.progress_bar.setFormat("100% (Merging...)")

        elif self.status in ["paused", "stopped"]:
            self.btn_pause.show()
            self.btn_cancel.show()
            self.btn_hide.show()
            self.btn_open.hide()
            self.btn_open_folder.hide()
            self.btn_close.hide()
            self.btn_pause.setText("Resume")
            self.status_label.setStyleSheet("font-weight: bold; color: #dd6b20;")
            self.status_label.setText("Paused")
            self.eta_label.setText("Paused")
            self.speed_label.setText("0 B/s")

        else:
            # Downloading / Active / Queued
            self.btn_pause.show()
            self.btn_cancel.show()
            self.btn_hide.show()
            self.btn_open.hide()
            self.btn_open_folder.hide()
            self.btn_close.hide()
            self.btn_pause.setText("Pause")
            self.status_label.setStyleSheet("font-weight: bold; color: #48bb78;")
            self.status_label.setText("Downloading")

    def update_segments(self, segments: List[dict]):
        self.segment_visualizer.set_segments(segments, self.total_bytes)

    def append_log(self, msg: str):
        self.log_text.appendPlainText(msg)

    def _on_pause_toggle(self):
        if self.status == "downloading":
            self.status = "paused"
            self.btn_pause.setText("Resume")
            self.status_label.setText("Paused")
            self.status_label.setStyleSheet("font-weight: bold; color: #dd6b20;")
            self.eta_label.setText("Paused")
            self.speed_label.setText("0 B/s")
            self.pause_requested.emit(self.download_id)
        else:
            self.status = "downloading"
            self.btn_pause.setText("Pause")
            self.status_label.setText("Downloading")
            self.status_label.setStyleSheet("font-weight: bold; color: #48bb78;")
            self.resume_requested.emit(self.download_id)

    def _on_cancel(self):
        self.cancel_requested.emit(self.download_id)
        self.reject()

    def _on_open_file(self):
        if self.save_path and os.path.exists(self.save_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.save_path))
        self.accept()

    def _on_open_folder(self):
        if self.save_path:
            dir_path = os.path.dirname(self.save_path)
            if os.path.exists(dir_path):
                QDesktopServices.openUrl(QUrl.fromLocalFile(dir_path))

    def _on_limit_toggled(self, checked: bool):
        self.limit_slider.setEnabled(checked)
        self.limit_val_label.setEnabled(checked)
        if checked:
            bps = self.limit_slider.value() * 1024
            self.speed_limit_changed.emit(self.download_id, bps)
        else:
            self.speed_limit_changed.emit(self.download_id, 0)

    def _on_limit_changed(self, value: int):
        bps = value * 1024
        self.limit_val_label.setText(f"{format_bytes(bps)}/s")
        if self.limit_check.isChecked():
            self.speed_limit_changed.emit(self.download_id, bps)
