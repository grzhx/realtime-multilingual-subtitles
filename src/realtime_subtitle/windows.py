"""Typed Windows APIs for Tk wrapper windows, without replacing WNDPROC."""
import ctypes
import sys
from ctypes import wintypes

TRANSPARENT = 0x20
NOACTIVATE = 0x08000000
HWND_TOPMOST = -1


class WindowStyles:
    def __init__(self):
        self.api = None
        if sys.platform == 'win32':
            self.api = ctypes.WinDLL('user32', use_last_error=True)
            self.api.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            self.api.GetAncestor.restype = wintypes.HWND
            self.api.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            self.api.GetWindowLongPtrW.restype = ctypes.c_ssize_t
            self.api.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
            self.api.SetWindowLongPtrW.restype = ctypes.c_ssize_t
            self.api.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                               ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
            self.api.SetWindowPos.restype = wintypes.BOOL

    def hwnd(self, widget):
        return self.api.GetAncestor(widget.winfo_id(), 2) if self.api else widget.winfo_id()

    def get(self, widget):
        return self.api.GetWindowLongPtrW(self.hwnd(widget), -20) if self.api else 0

    def click_through(self, widget, enabled):
        if not self.api:
            return
        hwnd = self.hwnd(widget)
        style = self.api.GetWindowLongPtrW(hwnd, -20)
        bits = TRANSPARENT | NOACTIVATE
        style = style | bits if enabled else style & ~bits
        ctypes.set_last_error(0)
        self.api.SetWindowLongPtrW(hwnd, -20, style)
        if ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())
        # FRAMECHANGED, NOMOVE, NOSIZE, NOZORDER, NOACTIVATE.
        if not self.api.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x37):
            raise ctypes.WinError(ctypes.get_last_error())

    def raise_topmost(self, widget):
        """Promote to the front of the topmost band without activation.

        Passing another HWND to SetWindowPos inserts BELOW it, not above it.
        Promote layers from back to front using HWND_TOPMOST instead.
        """
        if not self.api:
            return
        hwnd = self.hwnd(widget)
        if not self.api.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                     0x0001 | 0x0002 | 0x0010 | 0x0200):
            raise ctypes.WinError(ctypes.get_last_error())

    def detach_owner(self, widget):
        """An owned popup is always above its owner, so background must be unowned."""
        if not self.api:
            return
        hwnd = self.hwnd(widget)
        if self.api.GetWindowLongPtrW(hwnd, -8):  # GWLP_HWNDPARENT (owner for popups)
            ctypes.set_last_error(0)
            self.api.SetWindowLongPtrW(hwnd, -8, 0)
            if ctypes.get_last_error():
                raise ctypes.WinError(ctypes.get_last_error())

    def move_resize(self, widget, x, y, width, height):
        """Move a top-level window in screen coordinates, bypassing Tk geometry."""
        if not self.api:
            return
        hwnd = self.hwnd(widget)
        # SWP_NOACTIVATE | SWP_NOZORDER | SWP_FRAMECHANGED.
        if not self.api.SetWindowPos(hwnd, None, int(x), int(y), int(width), int(height), 0x04 | 0x10 | 0x20):
            raise ctypes.WinError(ctypes.get_last_error())
