APP_STYLESHEET = """
QWidget {
    color: #243047;
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
    font-size: 13px;
}
QMainWindow, QDialog {
    background: #f4f7fb;
}
QFrame#Sidebar {
    background: #17233c;
    border: none;
}
QLabel#BrandTitle {
    color: white;
    font-size: 20px;
    font-weight: 700;
}
QLabel#BrandSubTitle, QLabel#SidebarUser {
    color: #aebbd1;
}
QPushButton#NavButton {
    color: #cbd5e5;
    background: transparent;
    border: none;
    border-radius: 8px;
    padding: 11px 14px;
    text-align: left;
    font-size: 14px;
}
QPushButton#NavButton:hover {
    background: #243453;
    color: white;
}
QPushButton#NavButton:checked {
    background: #3478f6;
    color: white;
    font-weight: 700;
}
QFrame#TopBar, QFrame#Card, QGroupBox {
    background: white;
    border: 1px solid #e4eaf2;
    border-radius: 10px;
}
QFrame#TopBar {
    border-radius: 0;
    border-left: none;
    border-right: none;
    border-top: none;
}
QFrame#Card {
    padding: 8px;
}
QLabel#PageTitle {
    font-size: 21px;
    font-weight: 700;
    color: #17233c;
}
QLabel#Muted {
    color: #708096;
}
QLabel#MetricValue {
    color: #1c5ed6;
    font-size: 28px;
    font-weight: 700;
}
QGroupBox {
    margin-top: 12px;
    padding: 14px 10px 10px 10px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 5px;
}
QLineEdit, QComboBox, QTextEdit, QTableWidget, QTableView {
    background: white;
    border: 1px solid #d9e1ec;
    border-radius: 6px;
    padding: 6px;
    selection-background-color: #3478f6;
}
QLineEdit:focus, QComboBox:focus, QTextEdit:focus {
    border: 1px solid #3478f6;
}
QPushButton {
    background: #eef3fa;
    border: 1px solid #d9e1ec;
    border-radius: 6px;
    padding: 7px 14px;
}
QPushButton:hover {
    background: #e2eaf5;
}
QPushButton#ViolationModeButton:checked {
    color: white;
    background: #3478f6;
    border-color: #3478f6;
    font-weight: 600;
}
QPushButton#ViolationModeButton:checked:hover {
    background: #2868db;
}
QPushButton#PrimaryButton {
    color: white;
    background: #3478f6;
    border-color: #3478f6;
    font-weight: 600;
}
QPushButton#PrimaryButton:hover {
    background: #2868db;
}
QPushButton#DangerButton {
    color: white;
    background: #e45454;
    border-color: #e45454;
}
QHeaderView::section {
    background: #f1f5fa;
    color: #526177;
    border: none;
    border-bottom: 1px solid #dce4ef;
    padding: 8px;
    font-weight: 600;
}
QTableWidget, QTableView {
    gridline-color: #edf1f6;
    alternate-background-color: #f8fafd;
}
QTabWidget#DashboardTabs::pane {
    background: white;
    border: 1px solid #dfe6ef;
    border-radius: 9px;
    top: -1px;
}
QTabWidget#DashboardTabs QTabBar::tab {
    color: #64748b;
    background: #eaf0f7;
    border: 1px solid #d9e2ee;
    border-bottom: none;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
    min-width: 118px;
    padding: 9px 18px;
    margin-right: 4px;
}
QTabWidget#DashboardTabs QTabBar::tab:hover {
    color: #245fc7;
    background: #f1f5fb;
}
QTabWidget#DashboardTabs QTabBar::tab:selected {
    color: #1c5ed6;
    background: white;
    border-color: #cfd9e7;
    font-weight: 700;
}
QProgressBar {
    border: 1px solid #d9e1ec;
    border-radius: 6px;
    background: white;
    text-align: center;
    min-height: 18px;
}
QProgressBar::chunk {
    background: #38b779;
    border-radius: 5px;
}
QScrollBar:vertical {
    background: #f1f4f8;
    width: 10px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #bcc8d8;
    border-radius: 5px;
    min-height: 28px;
}
"""
