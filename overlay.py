import sys
import json
import os
import ctypes
from ctypes import wintypes # 尽管移除了全局热键，但GifOverlay的鼠标穿透仍依赖ctypes
import traceback
import datetime

from PyQt6.QtWidgets import (
    QApplication, QLabel, QPushButton, QVBoxLayout,
    QWidget, QListWidget, QFileDialog, QListWidgetItem, QDialog,
    QHBoxLayout, QMessageBox, QSlider, QLineEdit, QFormLayout, QGroupBox,
    QCheckBox, QInputDialog # 移除了 QKeySequenceEdit
)
from PyQt6.QtGui import (
    QMovie, QPixmap, QImage, QPainter, QIcon, QDoubleValidator,
    QImageReader, QKeySequence # QKeySequence 现在不再用于全局热键，但可能用于其他地方或保留为PyQt内置功能
)
from PyQt6.QtCore import Qt, QPoint, QSize, QEvent, pyqtSignal, QCoreApplication

# ------------------ 常量 ------------------
ASSET_FOLDER = "assets"
ERROR_LOG_FILE = "error.log"
ICON_FILE = "icon.ico"          # 请准备 icon.ico
SUPPORTED_FORMATS = (".gif", ".webp", ".png", ".jpg", ".jpeg")
CONFIGS_FOLDER = "configs"

# 移除了所有与全局热键相关的 Windows API 常量

def log_error(message):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {message}\n")

# ================== 单个悬浮窗（独立实例） ==================
class GifOverlay(QLabel):
    opacityChanged = pyqtSignal(float)
    sizeChanged = pyqtSignal(int, int)

    def __init__(self, file_path: str = ""):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet("background-color: transparent;")
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

        self.movie = QMovie()
        self.current_frame_pixmap = QPixmap()
        self.movie_playing = False
        self.drag_pos = None
        self.locked = False
        self.border = 6
        self.min_size = QSize(20, 20)
        self.max_size = QSize(5000, 5000)

        self._current_opacity = 1.0
        self.setWindowOpacity(self._current_opacity)
        self.mouse_transparent = False
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)

        self.file_path = ""
        self.size_cache = {"w": 300, "h": 300}
        self.pos_cache = {"x": 200, "y": 200}
        self._updating_position = False

        if file_path:
            self.set_animation_file(file_path)
        else:
            self.resize(300, 300)
            self.move(200, 200)

    # ---------- Windows 原生事件（保持原样，仅用于鼠标穿透和窗口调整大小） ----------
    def nativeEvent(self, eventType, message):
        if eventType != b"windows_generic_MSG":
            return False, 0
        msg = ctypes.wintypes.MSG.from_address(int(message))
        WM_NCHITTEST = 0x0084
        HTTRANSPARENT = -1
        HTBOTTOMRIGHT = 17
        HTBOTTOMLEFT = 16
        HTTOPRIGHT = 14
        HTTOPLEFT = 13
        HTRIGHT = 11
        HTLEFT = 10
        HTBOTTOM = 15
        HTTOP = 12
        HTCAPTION = 2
        HTCLIENT = 1

        if msg.message == WM_NCHITTEST:
            if self.mouse_transparent:
                return True, HTTRANSPARENT
            x = ctypes.c_short(msg.lParam & 0xFFFF).value
            y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
            rect = self.geometry()
            left, top, right, bottom = rect.x(), rect.y(), rect.x() + rect.width(), rect.y() + rect.height()
            border = self.border

            if self.locked:
                return True, HTCLIENT

            if x >= right - border and y >= bottom - border:
                return True, HTBOTTOMRIGHT
            if x <= left + border and y >= bottom - border:
                return True, HTBOTTOMLEFT
            if x >= right - border and y <= top + border:
                return True, HTTOPRIGHT
            if x <= left + border and y <= top + border:
                return True, HTTOPLEFT
            if x >= right - border:
                return True, HTRIGHT
            if x <= left + border:
                return True, HTLEFT
            if y >= bottom - border:
                return True, HTBOTTOM
            if y <= top + border:
                return True, HTTOP
            return True, HTCAPTION if not self.locked else HTCLIENT
        return False, 0

    def mousePressEvent(self, event):
        if self.mouse_transparent or event.button() != Qt.MouseButton.LeftButton or self.locked:
            return
        self.drag_pos = event.globalPosition().toPoint()

    def mouseMoveEvent(self, event):
        if self.mouse_transparent or not self.drag_pos or self.locked:
            return
        diff = event.globalPosition().toPoint() - self.drag_pos
        self.move(self.x() + diff.x(), self.y() + diff.y())
        self.drag_pos = event.globalPosition().toPoint()

    def mouseReleaseEvent(self, event):
        if self.mouse_transparent:
            return
        self.drag_pos = None
        self.update_current_position()

    def moveEvent(self, event):
        super().moveEvent(event)
        if not self._updating_position:
            self.update_current_position()

    def resizeEvent(self, event):
        new_w = max(self.min_size.width(), min(event.size().width(), self.max_size.width()))
        new_h = max(self.min_size.height(), min(event.size().height(), self.max_size.height()))
        if new_w != event.size().width() or new_h != event.size().height():
            self.resize(new_w, new_h)
            if not self.current_frame_pixmap.isNull():
                self._scale_and_set_frame(self.current_frame_pixmap.toImage())
                self.update()
        super().resizeEvent(event)
        self.update_current_size()
        self.sizeChanged.emit(self.width(), self.height())

    def _scale_and_set_frame(self, image: QImage):
        if image.isNull():
            self.current_frame_pixmap = QPixmap()
            return
        scaled = image.scaled(self.size(), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.current_frame_pixmap = QPixmap.fromImage(scaled)
        self.update()

    def update_frame(self):
        if not self.movie or not self.movie_playing:
            self.current_frame_pixmap = QPixmap()
            self.update()
            return
        img = self.movie.currentImage()
        if not img.isNull():
            self._scale_and_set_frame(img)
        else:
            self.current_frame_pixmap = QPixmap()
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        if not self.current_frame_pixmap.isNull():
            painter.drawPixmap(self.rect(), self.current_frame_pixmap)
        painter.end()

    # ---------- 鼠标穿透 ----------
    def _set_window_ex_transparent(self, enable: bool):
        try:
            hwnd = int(self.winId())
            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x00000020
            user32 = ctypes.windll.user32
            ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if enable:
                ex_style |= WS_EX_TRANSPARENT
            else:
                ex_style &= ~WS_EX_TRANSPARENT
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style)
            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0002 | 0x0001)
        except Exception as e:
            log_error(f"Set WS_EX_TRANSPARENT failed: {e}")

    def toggle_mouse_transparent(self):
        self.mouse_transparent = not self.mouse_transparent
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, self.mouse_transparent)
        self._set_window_ex_transparent(self.mouse_transparent)
        return self.mouse_transparent

    # ---------- 尺寸与位置 ----------
    def update_current_size(self):
        self.size_cache = {"w": self.width(), "h": self.height()}

    def update_current_position(self):
        self.pos_cache = {"x": self.x(), "y": self.y()}

    def apply_size(self, w, h):
        w = max(self.min_size.width(), min(w, self.max_size.width()))
        h = max(self.min_size.height(), min(h, self.max_size.height()))
        self.resize(w, h)
        self.update_current_size()

    def apply_position(self, x, y):
        self._updating_position = True
        self.move(x, y)
        self._updating_position = False
        self.update_current_position()

    # ---------- 文件加载 ----------
    def set_animation_file(self, file_path: str):
        if not file_path or not os.path.exists(file_path):
            log_error(f"File not found: {file_path}")
            if self.movie:
                self.movie.stop()
            self.current_frame_pixmap = QPixmap()
            self.update()
            self.movie_playing = False
            self.file_path = ""
            return False

        self.file_path = file_path
        if self.movie:
            self.movie.stop()
            try:
                self.movie.frameChanged.disconnect(self.update_frame)
            except TypeError:
                pass
        self.movie.setFileName(file_path)
        if not self.movie.isValid():
            log_error(f"Invalid movie: {file_path}")
            self.movie = QMovie()
            self.current_frame_pixmap = QPixmap()
            self.update()
            self.movie_playing = False
            return False

        self.apply_size(self.size_cache["w"], self.size_cache["h"])
        self.apply_position(self.pos_cache["x"], self.pos_cache["y"])

        is_anim = self.movie.frameCount() > 1
        self.movie.start()
        self.movie_playing = True
        if is_anim:
            self.movie.frameChanged.connect(self.update_frame)
        else:
            self.movie.setPaused(True)
            self.update_frame()
        return True

    def toggle_play_pause(self):
        if not self.movie or not self.movie.isValid():
            return False
        if self.movie_playing:
            self.movie.setPaused(True)
            self.current_frame_pixmap = QPixmap()
            self.update()
            self.movie_playing = False
        else:
            self.movie.setPaused(False)
            self.update_frame()
            self.movie_playing = True
        return self.movie_playing

    def set_speed(self, speed: float):
        if self.movie and self.movie.isValid():
            self.movie.setSpeed(int(speed * 100))

    def get_speed(self) -> float:
        if self.movie and self.movie.isValid():
            return self.movie.speed() / 100.0
        return 1.0

    def set_opacity(self, opacity: float):
        self._current_opacity = max(0.0, min(1.0, opacity))
        self.setWindowOpacity(self._current_opacity)
        self.opacityChanged.emit(self._current_opacity)

    def get_opacity(self) -> float:
        return self._current_opacity

# ================== 控制面板 ==================
class ControlPanel(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("巴扎串串 - 多图悬浮置顶助手 V2.0")
        self.setGeometry(150, 150, 850, 700)
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint |
            Qt.WindowType.WindowMinimizeButtonHint
        )
        if os.path.exists(ICON_FILE):
            self.setWindowIcon(QIcon(ICON_FILE))

        # 核心数据
        self.overlays = {}          # 绝对路径 -> GifOverlay
        self.checked_files = set()  # 当前勾选的文件绝对路径
        self.current_selected_file = None

        # 全局状态
        self.global_visible = True
        self.global_locked = False
        self.global_transparent = False
        self.global_playing = True

        # 移除了所有与快捷键相关的属性

        self._init_ui()
        self._load_all_files()          # 显示所有文件
        self._load_asset_folders()      # 加载文件夹导航
        self._update_ui_states() # 初始状态更新

    # 移除了 nativeEvent 方法，因为它不再需要处理 WM_HOTKEY

    def _init_ui(self):
        main_layout = QVBoxLayout(self)

        # ---- 全局控制 ----
        global_layout = QHBoxLayout()
        self.play_pause_button = QPushButton("暂停动画")
        self.play_pause_button.clicked.connect(self._toggle_global_play_pause)
        global_layout.addWidget(self.play_pause_button)

        self.visibility_button = QPushButton("隐藏")
        self.visibility_button.clicked.connect(self._toggle_global_visibility)
        global_layout.addWidget(self.visibility_button)

        self.lock_button = QPushButton("锁定")
        self.lock_button.clicked.connect(self._toggle_global_lock)
        global_layout.addWidget(self.lock_button)

        self.transparent_button = QPushButton("开启鼠标穿透")
        self.transparent_button.clicked.connect(self._toggle_global_transparent)
        global_layout.addWidget(self.transparent_button)

        main_layout.addLayout(global_layout)
        main_layout.addSpacing(10)

        # ---- 文件夹导航 + 文件列表（全局显示） ----
        nav_layout = QHBoxLayout()

        # 左侧：文件夹列表（仅用于定位）
        folder_group = QGroupBox("快速定位")
        folder_vbox = QVBoxLayout(folder_group)
        self.folder_list_widget = QListWidget()
        self.folder_list_widget.setMaximumWidth(200)
        self.folder_list_widget.itemClicked.connect(self._on_folder_clicked)
        folder_vbox.addWidget(self.folder_list_widget)
        nav_layout.addWidget(folder_group)

        # 右侧：全局文件列表（显示所有文件）
        file_group = QGroupBox("素材文件（勾选即显示）")
        file_vbox = QVBoxLayout(file_group)
        self.file_list_widget = QListWidget()
        self.file_list_widget.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.file_list_widget.itemSelectionChanged.connect(self._on_file_selected)
        self.file_list_widget.itemChanged.connect(self._on_item_check_state_changed)
        file_vbox.addWidget(self.file_list_widget)
        nav_layout.addWidget(file_group)

        main_layout.addLayout(nav_layout)
        main_layout.addSpacing(10)

        # ---- 底部：独立设置 + 配置管理 ----
        bottom_layout = QHBoxLayout()

        # 独立设置
        settings_group = QGroupBox("当前文件独立设置")
        settings_form = QFormLayout(settings_group)

        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(20, 500)
        self.speed_slider.setValue(100)
        self.speed_slider.valueChanged.connect(self._on_speed_slider_changed)
        self.speed_input = QLineEdit()
        self.speed_input.setValidator(QDoubleValidator(0.2, 5.0, 1))
        self.speed_input.setText("1.0")
        self.speed_input.editingFinished.connect(self._on_speed_input_edited)
        speed_layout = QHBoxLayout()
        speed_layout.addWidget(self.speed_slider)
        speed_layout.addWidget(self.speed_input)
        speed_layout.addWidget(QLabel("x"))
        settings_form.addRow("播放速度:", speed_layout)

        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(100)
        self.opacity_slider.valueChanged.connect(self._on_opacity_slider_changed)
        self.opacity_input = QLineEdit()
        self.opacity_input.setValidator(QDoubleValidator(0.0, 1.0, 2))
        self.opacity_input.setText("1.0")
        self.opacity_input.editingFinished.connect(self._on_opacity_input_edited)
        opacity_layout = QHBoxLayout()
        opacity_layout.addWidget(self.opacity_slider)
        opacity_layout.addWidget(self.opacity_input)
        settings_form.addRow("透明度:", opacity_layout)

        self.width_input = QLineEdit()
        self.width_input.setValidator(QDoubleValidator(20, 5000, 0))
        self.width_input.editingFinished.connect(self._on_size_input_edited)
        settings_form.addRow("宽度:", self.width_input)

        self.height_input = QLineEdit()
        self.height_input.setValidator(QDoubleValidator(20, 5000, 0))
        self.height_input.editingFinished.connect(self._on_size_input_edited)
        settings_form.addRow("高度:", self.height_input)

        self.refresh_button = QPushButton("刷新文件列表")
        self.refresh_button.clicked.connect(self._refresh_all)
        settings_form.addRow(self.refresh_button)

        formats_label = QLabel(f"<b>支持格式:</b> {', '.join([f.upper() for f in SUPPORTED_FORMATS])}")
        settings_form.addRow(formats_label)
        tip_label = QLabel("看到记得喝水和提肛 🐮")
        settings_form.addRow(tip_label)

        bottom_layout.addWidget(settings_group, stretch=2)

        # 配置管理
        config_group = QGroupBox("配置管理")
        config_vbox = QVBoxLayout(config_group)

        self.save_config_button = QPushButton("保存当前设定")
        self.save_config_button.clicked.connect(self._save_config)
        config_vbox.addWidget(self.save_config_button)

        self.load_config_button = QPushButton("加载设定")
        self.load_config_button.clicked.connect(self._load_config)
        config_vbox.addWidget(self.load_config_button)

        # 移除了快捷键隐藏的 QGroupBox 及其内容

        config_vbox.addStretch() # 填充空白

        # 添加作者信息在右下角
        author_label = QLabel("作者：江南牧猪人")
        author_label.setAlignment(Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)
        config_vbox.addWidget(author_label)


        bottom_layout.addWidget(config_group, stretch=1)

        main_layout.addLayout(bottom_layout)


    # ---------- 加载所有文件 ----------
    def _load_all_files(self):
        """扫描 assets 下所有子文件夹中的支持文件，填充文件列表"""
        self.file_list_widget.clear()
        if not os.path.exists(ASSET_FOLDER):
            os.makedirs(ASSET_FOLDER)
            return

        for root, dirs, files in os.walk(ASSET_FOLDER):
            for fname in files:
                if fname.lower().endswith(SUPPORTED_FORMATS):
                    abs_path = os.path.join(root, fname)
                    # 显示时带相对路径前缀，便于区分
                    rel_path = os.path.relpath(abs_path, ASSET_FOLDER)
                    display_name = rel_path
                    item = QListWidgetItem(display_name)
                    item.setData(Qt.ItemDataRole.UserRole, abs_path)
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    # 根据 checked_files 设置勾选状态
                    if abs_path in self.checked_files:
                        item.setCheckState(Qt.CheckState.Checked)
                    else:
                        item.setCheckState(Qt.CheckState.Unchecked)
                    self.file_list_widget.addItem(item)

    def _load_asset_folders(self):
        """加载 assets 下的子文件夹列表（用于导航）"""
        self.folder_list_widget.clear()
        if not os.path.exists(ASSET_FOLDER):
            os.makedirs(ASSET_FOLDER)
            return
        for folder in os.listdir(ASSET_FOLDER):
            folder_path = os.path.join(ASSET_FOLDER, folder)
            if os.path.isdir(folder_path):
                item = QListWidgetItem(folder)
                item.setData(Qt.ItemDataRole.UserRole, folder_path)
                self.folder_list_widget.addItem(item)

    def _refresh_all(self):
        """刷新所有列表，保留当前勾选状态"""
        self._load_asset_folders()
        self._load_all_files()
        # 恢复选中状态（如果有）
        if self.current_selected_file:
            for i in range(self.file_list_widget.count()):
                if self.file_list_widget.item(i).data(Qt.ItemDataRole.UserRole) == self.current_selected_file:
                    self.file_list_widget.setCurrentRow(i)
                    break

    # ---------- 文件夹导航（点击定位） ----------
    def _on_folder_clicked(self, item):
        """点击文件夹，滚动到该文件夹的第一个文件"""
        folder_path = item.data(Qt.ItemDataRole.UserRole)
        for i in range(self.file_list_widget.count()):
            file_path = self.file_list_widget.item(i).data(Qt.ItemDataRole.UserRole)
            if file_path.startswith(folder_path):
                self.file_list_widget.setCurrentRow(i)
                self.file_list_widget.scrollToItem(self.file_list_widget.item(i))
                break

    # ---------- 文件列表勾选处理 ----------
    def _on_item_check_state_changed(self, item: QListWidgetItem):
        file_path = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            # 勾选：添加到集合，显示 overlay
            self.checked_files.add(file_path)
            if file_path not in self.overlays:
                overlay = GifOverlay(file_path)
                # 应用全局状态
                overlay.locked = self.global_locked
                overlay.mouse_transparent = self.global_transparent
                overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, self.global_transparent)
                overlay._set_window_ex_transparent(self.global_transparent)
                if not self.global_visible:
                    overlay.hide()
                if not self.global_playing:
                    overlay.movie.setPaused(True)
                    overlay.movie_playing = False
                self.overlays[file_path] = overlay
            else:
                overlay = self.overlays[file_path]
            # 强制显示（根据全局可见性）
            if self.global_visible:
                overlay.show()
            else:
                overlay.hide()
            # 自动选中该项，使独立设置生效
            self.file_list_widget.setCurrentItem(item)
        else:
            # 取消勾选：从集合移除，隐藏 overlay
            self.checked_files.discard(file_path)
            if file_path in self.overlays:
                self.overlays[file_path].hide()
        # 更新当前选中状态（可能焦点变化）
        self._on_file_selected()

    # ---------- 文件选中（独立设置） ----------
    def _on_file_selected(self):
        selected = self.file_list_widget.selectedItems()
        if selected:
            file_path = selected[0].data(Qt.ItemDataRole.UserRole)
            self.current_selected_file = file_path
            overlay = self.overlays.get(file_path)
            if overlay and file_path in self.checked_files:
                # 更新控件
                self.speed_slider.setValue(int(overlay.get_speed() * 100))
                self.speed_input.setText(f"{overlay.get_speed():.1f}")
                self.opacity_slider.setValue(int(overlay.get_opacity() * 100))
                self.opacity_input.setText(f"{overlay.get_opacity():.2f}")
                self.width_input.setText(str(overlay.width()))
                self.height_input.setText(str(overlay.height()))
            else:
                # 未勾选或没有overlay，控件保留上次值
                # 可以考虑禁用或清空
                self.speed_slider.setValue(100)
                self.speed_input.setText("1.0")
                self.opacity_slider.setValue(100)
                self.opacity_input.setText("1.0")
                self.width_input.setText("")
                self.height_input.setText("")
        else:
            self.current_selected_file = None
            # 清空所有独立设置UI
            self.speed_slider.setValue(100)
            self.speed_input.setText("1.0")
            self.opacity_slider.setValue(100)
            self.opacity_input.setText("1.0")
            self.width_input.setText("")
            self.height_input.setText("")


    # ---------- 独立设置控件响应 ----------
    def _apply_to_selected(self, func):
        if self.current_selected_file and self.current_selected_file in self.checked_files:
            overlay = self.overlays.get(self.current_selected_file)
            if overlay:
                func(overlay)

    def _on_speed_slider_changed(self, value):
        speed = value / 100.0
        self.speed_input.setText(f"{speed:.1f}")
        self._apply_to_selected(lambda ov: ov.set_speed(speed))

    def _on_speed_input_edited(self):
        try:
            speed = float(self.speed_input.text())
            speed = max(0.2, min(5.0, speed))
            self.speed_input.setText(f"{speed:.1f}")
            self.speed_slider.setValue(int(speed * 100))
            self._apply_to_selected(lambda ov: ov.set_speed(speed))
        except ValueError:
            # Revert to current value if input is invalid
            self._apply_to_selected(lambda ov: self.speed_input.setText(f"{ov.get_speed():.1f}"))
            if not self.current_selected_file or self.current_selected_file not in self.checked_files:
                 self.speed_input.setText("1.0")


    def _on_opacity_slider_changed(self, value):
        opacity = value / 100.0
        self.opacity_input.setText(f"{opacity:.2f}")
        self._apply_to_selected(lambda ov: ov.set_opacity(opacity))

    def _on_opacity_input_edited(self):
        try:
            opacity = float(self.opacity_input.text())
            opacity = max(0.0, min(1.0, opacity))
            self.opacity_input.setText(f"{opacity:.2f}")
            self.opacity_slider.setValue(int(opacity * 100))
            self._apply_to_selected(lambda ov: ov.set_opacity(opacity))
        except ValueError:
            # Revert to current value if input is invalid
            self._apply_to_selected(lambda ov: self.opacity_input.setText(f"{ov.get_opacity():.2f}"))
            if not self.current_selected_file or self.current_selected_file not in self.checked_files:
                self.opacity_input.setText("1.0")

    def _on_size_input_edited(self):
        try:
            w = int(float(self.width_input.text()))
            h = int(float(self.height_input.text()))
            self._apply_to_selected(lambda ov: ov.apply_size(w, h))
        except ValueError:
            # Revert to current value if input is invalid
            self._apply_to_selected(lambda ov: (self.width_input.setText(str(ov.width())), self.height_input.setText(str(ov.height()))))
            if not self.current_selected_file or self.current_selected_file not in self.checked_files:
                self.width_input.setText("")
                self.height_input.setText("")

    # ---------- 全局控制 ----------
    def _toggle_global_play_pause(self):
        self.global_playing = not self.global_playing
        for overlay in self.overlays.values():
            if overlay.isVisible():
                if self.global_playing:
                    overlay.movie.setPaused(False)
                    overlay.movie_playing = True
                else:
                    overlay.movie.setPaused(True)
                    overlay.movie_playing = False
        self._update_play_button_text()

    def _toggle_global_visibility(self):
        self.global_visible = not self.global_visible
        for file_path in self.checked_files: # 只影响当前勾选的
            overlay = self.overlays.get(file_path)
            if overlay:
                if self.global_visible:
                    overlay.show()
                else:
                    overlay.hide()
        self._update_visibility_button_text()

    def _toggle_global_lock(self):
        self.global_locked = not self.global_locked
        for file_path in self.checked_files: # 只影响当前勾选的
            overlay = self.overlays.get(file_path)
            if overlay:
                overlay.locked = self.global_locked
        self._update_lock_button_text()

    def _toggle_global_transparent(self):
        self.global_transparent = not self.global_transparent
        for file_path in self.checked_files: # 只影响当前勾选的
            overlay = self.overlays.get(file_path)
            if overlay:
                overlay.mouse_transparent = self.global_transparent
                overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, self.global_transparent)
                overlay._set_window_ex_transparent(self.global_transparent)
        self._update_transparent_button_text()

    # ---------- 配置保存与加载 ----------
    def _ensure_configs_folder(self):
        if not os.path.exists(CONFIGS_FOLDER):
            os.makedirs(CONFIGS_FOLDER)

    def _save_config(self):
        if not self.checked_files:
            QMessageBox.warning(self, "警告", "当前没有打开任何图片，无法保存配置。")
            return

        config_data = {
            "global": {
                "visible": self.global_visible,
                "locked": self.global_locked,
                "transparent": self.global_transparent,
                "playing": self.global_playing,
                # 移除了热键相关的配置项
            },
            "files": []
        }

        for file_path in self.checked_files:
            overlay = self.overlays.get(file_path)
            if overlay:
                rel_path = os.path.relpath(file_path, os.getcwd())
                config_data["files"].append({
                    "path": rel_path,
                    "x": overlay.pos_cache["x"],
                    "y": overlay.pos_cache["y"],
                    "w": overlay.size_cache["w"],
                    "h": overlay.size_cache["h"],
                    "speed": overlay.get_speed(),
                    "opacity": overlay.get_opacity()
                })

        name, ok = QInputDialog.getText(self, "保存配置", "请输入配置名称：")
        if not ok or not name.strip():
            return
        name = name.strip()
        self._ensure_configs_folder()
        config_path = os.path.join(CONFIGS_FOLDER, f"{name}.json")

        if os.path.exists(config_path):
            reply = QMessageBox.question(
                self, "确认覆盖",
                f"配置 '{name}' 已存在，是否覆盖？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config_data, f, indent=4, ensure_ascii=False)
            QMessageBox.information(self, "成功", f"配置已保存到：{config_path}")
        except Exception as e:
            log_error(f"保存配置失败: {e}")
            QMessageBox.critical(self, "错误", f"保存配置失败：{e}")

    def _load_config(self):
        self._ensure_configs_folder()
        config_path, _ = QFileDialog.getOpenFileName(
            self, "加载配置", CONFIGS_FOLDER, "JSON文件 (*.json)"
        )
        if not config_path:
            return

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
        except Exception as e:
            QMessageBox.critical(self, "错误", f"加载配置文件失败：{e}")
            return

        # 1. 清除所有当前显示
        self._clear_all_overlays()

        # 2. 应用全局状态
        g = config_data.get("global", {})
        self.global_visible = g.get("visible", True)
        self.global_locked = g.get("locked", False)
        self.global_transparent = g.get("transparent", False)
        self.global_playing = g.get("playing", True)
        # 移除了热键相关的加载

        # 3. 加载文件
        for file_item in config_data.get("files", []):
            rel_path = file_item.get("path", "")
            abs_path = os.path.join(os.getcwd(), rel_path)
            if not os.path.exists(abs_path):
                continue  # 静默跳过

            # 确保该文件在列表中（可能因刷新未加载，我们直接添加）
            self._ensure_file_in_list(abs_path)

            # 直接创建 overlay 并应用所有参数，不依赖勾选信号
            overlay = GifOverlay(abs_path)
            overlay.apply_position(file_item.get("x", 200), file_item.get("y", 200))
            overlay.apply_size(file_item.get("w", 300), file_item.get("h", 300))
            overlay.set_speed(file_item.get("speed", 1.0))
            overlay.set_opacity(file_item.get("opacity", 1.0))
            # 应用全局状态
            overlay.locked = self.global_locked
            overlay.mouse_transparent = self.global_transparent
            overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, self.global_transparent)
            overlay._set_window_ex_transparent(self.global_transparent)
            if self.global_visible:
                overlay.show()
            else:
                overlay.hide()
            if self.global_playing:
                overlay.movie.setPaused(False)
                overlay.movie_playing = True
            else:
                overlay.movie.setPaused(True)
                overlay.movie_playing = False

            self.overlays[abs_path] = overlay
            self.checked_files.add(abs_path)
            # 同步勾选框
            self._set_item_check_state(abs_path, Qt.CheckState.Checked)

        # 更新UI
        self._update_ui_states()
        # 移除了热键UI的更新
        # 如果有文件，选中第一个
        if self.file_list_widget.count() > 0:
            self.file_list_widget.setCurrentRow(0)
        self._on_file_selected() # 确保选中项的独立设置UI更新

    def _clear_all_overlays(self):
        """清除所有显示：隐藏并销毁所有 overlay，清空字典和集合，重置列表勾选"""
        for overlay in self.overlays.values():
            overlay.hide()
            overlay.deleteLater()
        self.overlays.clear()
        self.checked_files.clear()
        # 重置当前文件列表中的勾选框
        for i in range(self.file_list_widget.count()):
            self.file_list_widget.item(i).setCheckState(Qt.CheckState.Unchecked)

    def _ensure_file_in_list(self, abs_path):
        """如果文件不在列表中，则添加（用于加载配置时）"""
        for i in range(self.file_list_widget.count()):
            if self.file_list_widget.item(i).data(Qt.ItemDataRole.UserRole) == abs_path:
                return
        # 文件不在列表中，添加之
        # 注意: 这里使用 os.path.relpath(abs_path, ASSET_FOLDER) 而不是 os.getcwd()
        # 保持显示名称的逻辑与 _load_all_files 一致
        try:
            rel = os.path.relpath(abs_path, ASSET_FOLDER)
        except ValueError: # 如果文件不在 ASSET_FOLDER 下，直接显示完整路径
            rel = abs_path
            
        display = rel
        item = QListWidgetItem(display)
        item.setData(Qt.ItemDataRole.UserRole, abs_path)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Unchecked)
        self.file_list_widget.addItem(item)

    def _set_item_check_state(self, abs_path, state):
        # 暂时阻断信号，避免触发 _on_item_check_state_changed 造成二次处理
        self.file_list_widget.blockSignals(True)
        for i in range(self.file_list_widget.count()):
            item = self.file_list_widget.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == abs_path:
                item.setCheckState(state)
                break
        self.file_list_widget.blockSignals(False)

    # 移除了所有与快捷键注册/注销相关的函数 (_register_global_hotkey, _unregister_global_hotkey, _on_hotkey_enabled_toggled, _on_hotkey_changed)

    # ---------- UI 更新 ----------
    def _update_play_button_text(self):
        self.play_pause_button.setText("暂停动画" if self.global_playing else "播放动画")

    def _update_visibility_button_text(self):
        self.visibility_button.setText("隐藏" if self.global_visible else "显示")

    def _update_lock_button_text(self):
        self.lock_button.setText("解锁" if self.global_locked else "锁定")

    def _update_transparent_button_text(self):
        self.transparent_button.setText("关闭鼠标穿透" if self.global_transparent else "开启鼠标穿透")

    def _update_ui_states(self):
        self._update_play_button_text()
        self._update_visibility_button_text()
        self._update_lock_button_text()
        self._update_transparent_button_text()
        # 移除了热键UI的更新，因为相关控件已删除


    def closeEvent(self, event):
        # 移除了 _unregister_global_hotkey 的调用
        for overlay in self.overlays.values():
            overlay.close()
        event.accept()

# ================== 主程序 ==================
if __name__ == "__main__":
    def excepthook(exc_type, exc_value, exc_traceback):
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        error_message = f"[{timestamp}] Uncaught exception:\n"
        error_message += "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        try:
            with open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(error_message + "\n")
            QMessageBox.critical(None, "程序崩溃", f"程序意外崩溃，错误信息已记录到 {ERROR_LOG_FILE}。")
        except Exception:
            print(error_message)
        sys.__excepthook__(exc_type, exc_value, exc_traceback)

    sys.excepthook = excepthook

    app = QApplication(sys.argv)
    
    if os.path.exists(ICON_FILE):
        app.setWindowIcon(QIcon(ICON_FILE))

    if not os.path.exists(ASSET_FOLDER):
        try:
            os.makedirs(ASSET_FOLDER)
        except Exception as e:
            log_error(f"无法创建 assets 文件夹: {e}")
            QMessageBox.critical(None, "错误", "无法创建 assets 文件夹，程序退出。")
            sys.exit(1)

    control = ControlPanel()
    control.show()

    # 移除了热键初始注册的逻辑

    sys.exit(app.exec())