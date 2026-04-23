"""Entry point — python -m sshcat"""

import sys
import os

from PySide6 import QtWidgets, QtGui

from .main_window import MainWindow


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")

    # 设置应用图标 (跨平台: .ico / .png)
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    for icon_name in ["icon.ico", "icon.png"]:
        icon_path = os.path.join(base, icon_name)
        if os.path.exists(icon_path):
            app.setWindowIcon(QtGui.QIcon(icon_path))
            break

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
