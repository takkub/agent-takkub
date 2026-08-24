"""openviking_setup_dialog.py — OpenViking Setup Wizard (Wave 2,
`docs/plans/openviking-managed-local-2026-08-24/07_SETUP_WIZARD.md`).

The ov.conf field names/provider lists below are copied verbatim from the
real upstream config-format doc (github.com/volcengine/OpenViking/blob/
main/docs/en/guides/01-configuration.md, fetched 2026-08-24 for this wave —
see `docs/audit/2026-08-24-openviking-managed-local-phase0.md`):
`embedding.dense.{provider,api_base,api_key,model}`, `vlm.{provider,model,
api_key,api_base}`, `storage.{workspace,agfs.backend,vectordb.backend,
vectordb.name}`, `server.{host,port}`. Nothing here invents a field, a
model name, or an embedding dimension the wizard's own form doesn't collect
from the user (`07_SETUP_WIZARD.md`: "Do not invent API endpoints/model
names") — `embedding.dense.dimension`/`.input` are real fields too, but
since this wizard never asks the user for them, they're simply omitted
rather than guessed; `openviking-server doctor`/startup is what actually
validates the result.

Two-step flow, both blocking calls off the Qt thread (`_ResultThread`/
`_InstallThread`, same "QThread + one result-or-Exception signal" shape
`settings_knowledge_design._CallableThread` already established elsewhere
in this Settings subsystem — duplicated locally rather than imported,
matching that module's own precedent of `settings_core_v2.py` doing the
same):
  [Test Configuration] -> write ov.conf, then `openviking-server doctor`
      (best-effort — needs the binary; a clear "not installed yet" message
      if it isn't there rather than a stack trace).
  [Install & Start] -> write ov.conf, `manager.ensure_installed()`,
      `manager.start()`, each step reflected in a progress label so a
      multi-minute pip install never looks hung.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import cockpit_theme, config, openviking_settings
from ._win_console import SUBPROCESS_NO_WINDOW
from .core.context_sources import openviking_adapter
from .openviking import credentials as ov_credentials
from .openviking import installer
from .openviking import manager as ov_manager
from .openviking import port as ov_port

_DOCTOR_TIMEOUT_S = 30.0

EMBEDDING_PROVIDERS: tuple[str, ...] = (
    "openai",
    "azure",
    "volcengine",
    "vikingdb",
    "jina",
    "ollama",
    "gemini",
    "voyage",
    "dashscope",
    "minimax",
    "cohere",
    "litellm",
    "local",
)
VLM_PROVIDERS: tuple[str, ...] = (
    "volcengine",
    "openai",
    "openai-codex",
    "kimi",
    "glm",
    "litellm",
)


def build_ov_conf(
    *,
    embedding_provider: str,
    embedding_api_base: str,
    embedding_api_key: str,
    embedding_model: str,
    vlm_provider: str,
    vlm_model: str,
    workspace_dir,
    port: int,
) -> dict:
    """Pure — no I/O, easy to unit test directly. The same API base/key are
    reused for both `embedding.dense` and `vlm` (the wizard's own form only
    collects one API Base/API Key pair, per `07_SETUP_WIZARD.md`'s
    wireframe — a real, still-correct value under the real schema, not an
    invented field).

    The API key itself is never written into the returned dict — only the
    literal `${OPENVIKING_API_KEY}` env-var placeholder upstream's config
    format already supports for `api_key` fields (`openviking.credentials`
    module docstring). `write_ov_conf` is the caller responsible for
    actually persisting the real value, into `SecretManager` rather than
    this file (HIGH finding, `docs/audit/2026-08-24-openviking-managed-
    review.md`)."""
    dense: dict = {"provider": embedding_provider, "model": embedding_model}
    if embedding_api_base:
        dense["api_base"] = embedding_api_base
    if embedding_api_key:
        dense["api_key"] = ov_credentials.API_KEY_PLACEHOLDER

    vlm: dict = {"provider": vlm_provider, "model": vlm_model}
    if embedding_api_base:
        vlm["api_base"] = embedding_api_base
    if embedding_api_key:
        vlm["api_key"] = ov_credentials.API_KEY_PLACEHOLDER

    return {
        "embedding": {"dense": dense},
        "vlm": vlm,
        "storage": {
            "workspace": str(workspace_dir),
            "agfs": {"backend": "local"},
            "vectordb": {"backend": "local", "name": "context"},
        },
        "server": {"host": "127.0.0.1", "port": port},
    }


def write_ov_conf(data: dict, *, api_key: str | None = None) -> None:
    """Blocking file I/O — off the Qt thread only. `installer.py`'s own
    docstring calls ov.conf's content "the Setup Wizard's job" — this is
    that job; `installer.py` only ever owns the path.

    *api_key*, when given, is the real secret `build_ov_conf` replaced
    with `${OPENVIKING_API_KEY}` in *data* — persisted separately into
    `SecretManager` so it never lands in `ov.conf` at rest (HIGH finding,
    `docs/audit/2026-08-24-openviking-managed-review.md`)."""
    installer.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config._write_json_atomic(installer.CONFIG_FILE, data)
    if api_key:
        ov_credentials.save_api_key(api_key)


def run_doctor(config_path) -> str:
    """`openviking-server doctor --config <path>` — blocking, off the Qt
    thread only. Raises when the binary isn't installed yet or the process
    itself fails; callers surface the exception text (fail-open UI
    convention already used throughout this Settings subsystem)."""
    exe = installer.server_executable()
    if not exe.is_file():
        raise RuntimeError("openviking-server ยังไม่ได้ติดตั้ง — กด “Install & Start” ก่อน")
    proc = subprocess.run(
        [str(exe), "doctor", "--config", str(config_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=SUBPROCESS_NO_WINDOW,
        timeout=_DOCTOR_TIMEOUT_S,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=ov_credentials.subprocess_env(),
    )
    return proc.stdout or f"(no output, exit code {proc.returncode})"


class _ResultThread(QThread):
    resultReady: pyqtSignal = pyqtSignal(object)

    def __init__(self, fn, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            self.resultReady.emit(self._fn())
        except Exception as e:  # pragma: no cover - fail-open, surfaced in the UI
            self.resultReady.emit(e)


class _InstallThread(QThread):
    stepChanged: pyqtSignal = pyqtSignal(str)
    resultReady: pyqtSignal = pyqtSignal(object)

    def __init__(self, conf: dict, api_key: str | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._conf = conf
        self._api_key = api_key

    def run(self) -> None:
        try:
            self.stepChanged.emit("กำลังบันทึก configuration…")
            write_ov_conf(self._conf, api_key=self._api_key)
            self.stepChanged.emit("กำลังติดตั้ง OpenViking (อาจใช้เวลาสักครู่)…")
            if not ov_manager.get_manager().ensure_installed():
                self.resultReady.emit(RuntimeError("ติดตั้งไม่สำเร็จ — ดู log ของ pip install"))
                return
            self.stepChanged.emit("กำลังเริ่ม OpenViking server…")
            status = ov_manager.get_manager().start()
            self.resultReady.emit(status)
        except Exception as e:  # pragma: no cover - fail-open, surfaced in the UI
            self.resultReady.emit(e)


class OpenVikingSetupDialog(QDialog):
    """`07_SETUP_WIZARD.md`. Opened from Settings › Knowledge & Design ›
    OpenViking's "Install & Enable" button (`settings_knowledge_design.
    KnowledgeDesignSettingsMixin._on_kd_ov_install_enable_clicked`)."""

    def __init__(self, parent: QWidget | None = None, *, fonts: dict | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("OpenViking Setup")
        if parent is not None:
            self.setStyleSheet(parent.styleSheet())
        self.resize(480, 520)
        self._fonts = fonts or {"mono": "monospace"}
        self._doctor_thread: _ResultThread | None = None
        self._install_thread: _InstallThread | None = None

        lay = QVBoxLayout(self)

        self._enable_check = QCheckBox("Enable local OpenViking", self)
        self._enable_check.setChecked(True)
        lay.addWidget(self._enable_check)

        runtime_lbl = QLabel("Runtime: Managed automatically by Takkub", self)
        runtime_lbl.setObjectName("panelHint")
        lay.addWidget(runtime_lbl)

        form = QFormLayout()
        self._embed_provider_combo = QComboBox(self)
        self._embed_provider_combo.addItems(EMBEDDING_PROVIDERS)
        form.addRow("Embedding Provider", self._embed_provider_combo)
        self._embed_api_base_edit = QLineEdit(self)
        self._embed_api_base_edit.setPlaceholderText("https://... (ไม่ต้องกรอกสำหรับ ollama)")
        form.addRow("API Base", self._embed_api_base_edit)
        self._embed_api_key_edit = QLineEdit(self)
        self._embed_api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._embed_api_key_edit.setPlaceholderText("ไม่ต้องกรอกสำหรับ ollama")
        form.addRow("API Key", self._embed_api_key_edit)
        self._embed_model_edit = QLineEdit(self)
        form.addRow("Embedding Model", self._embed_model_edit)

        self._vlm_provider_combo = QComboBox(self)
        self._vlm_provider_combo.addItems(VLM_PROVIDERS)
        form.addRow("VLM/Content Provider", self._vlm_provider_combo)
        self._vlm_model_edit = QLineEdit(self)
        form.addRow("Model", self._vlm_model_edit)
        lay.addLayout(form)

        action_row = QHBoxLayout()
        self._test_btn = cockpit_theme.secondary_button("Test Configuration", self)
        self._test_btn.clicked.connect(self._on_test_clicked)
        action_row.addWidget(self._test_btn)
        self._install_btn = cockpit_theme.gold_button("Install & Start", self)
        self._install_btn.clicked.connect(self._on_install_clicked)
        action_row.addWidget(self._install_btn)
        action_row.addStretch(1)
        lay.addLayout(action_row)

        self._progress_lbl = QLabel("", self)
        self._progress_lbl.setObjectName("panelHint")
        self._progress_lbl.setWordWrap(True)
        lay.addWidget(self._progress_lbl)

        self._result_box = QPlainTextEdit(self)
        self._result_box.setReadOnly(True)
        self._result_box.setStyleSheet(f'font-family: "{self._fonts["mono"]}"; font-size: 12px;')
        self._result_box.setFixedHeight(120)
        lay.addWidget(self._result_box)

        adv_row = QHBoxLayout()
        adv_row.addWidget(QLabel("Advanced:", self))
        run_doctor_btn = cockpit_theme.secondary_button("Run Doctor", self)
        run_doctor_btn.clicked.connect(self._on_test_clicked)
        adv_row.addWidget(run_doctor_btn)
        open_config_btn = cockpit_theme.secondary_button("Open Config", self)
        open_config_btn.clicked.connect(self._on_open_config_clicked)
        adv_row.addWidget(open_config_btn)
        adv_row.addStretch(1)
        lay.addLayout(adv_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        lay.addWidget(buttons)

    def _current_conf(self) -> dict:
        return build_ov_conf(
            embedding_provider=self._embed_provider_combo.currentText(),
            embedding_api_base=self._embed_api_base_edit.text().strip(),
            embedding_api_key=self._embed_api_key_edit.text().strip(),
            embedding_model=self._embed_model_edit.text().strip(),
            vlm_provider=self._vlm_provider_combo.currentText(),
            vlm_model=self._vlm_model_edit.text().strip(),
            workspace_dir=installer.DATA_DIR,
            port=ov_port.DEFAULT_PORT,
        )

    def _current_api_key(self) -> str | None:
        return self._embed_api_key_edit.text().strip() or None

    def _set_buttons_enabled(self, enabled: bool) -> None:
        self._test_btn.setEnabled(enabled)
        self._install_btn.setEnabled(enabled)

    def _on_test_clicked(self) -> None:
        self._set_buttons_enabled(False)
        self._result_box.setPlainText("กำลังทดสอบ…")
        try:
            write_ov_conf(self._current_conf(), api_key=self._current_api_key())
        except Exception as e:
            self._set_buttons_enabled(True)
            self._result_box.setPlainText(f"บันทึก config ไม่สำเร็จ: {e}")
            return
        thread = _ResultThread(lambda: run_doctor(installer.CONFIG_FILE), self)
        thread.resultReady.connect(self._on_test_ready)
        self._doctor_thread = thread
        thread.start()

    def _on_test_ready(self, result: object) -> None:
        self._set_buttons_enabled(True)
        self._result_box.setPlainText(str(result))

    def _on_open_config_clicked(self) -> None:
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        installer.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(installer.CONFIG_DIR)))

    def _on_install_clicked(self) -> None:
        self._set_buttons_enabled(False)
        self._progress_lbl.setText("เริ่มติดตั้ง…")
        self._result_box.setPlainText("")
        thread = _InstallThread(self._current_conf(), self._current_api_key(), self)
        thread.stepChanged.connect(self._progress_lbl.setText)
        thread.resultReady.connect(self._on_install_ready)
        self._install_thread = thread
        thread.start()

    def _on_install_ready(self, result: object) -> None:
        self._set_buttons_enabled(True)
        if isinstance(result, Exception):
            self._progress_lbl.setText("ติดตั้งไม่สำเร็จ")
            self._result_box.setPlainText(str(result))
            return

        status: ov_manager.ManagerStatus = result
        if self._enable_check.isChecked():
            # setdefault, not an unconditional set — an explicit env var the
            # user set themselves in their own shell still wins (same rule
            # `manager.boot_wiring` applies).
            os.environ.setdefault(openviking_adapter._ENV_ENABLED, "1")
            cfg = openviking_settings.load()
            openviking_settings.save(
                dataclasses.replace(cfg, enabled=True, start_automatically=True)
            )

        if status.healthy:
            self._progress_lbl.setText("พร้อมใช้งาน")
            self._result_box.setPlainText(f"OpenViking กำลังทำงานที่ {status.url}")
        else:
            self._progress_lbl.setText("ติดตั้งเสร็จแต่ยังไม่ healthy")
            self._result_box.setPlainText(status.error or "ไม่ทราบสาเหตุ")
