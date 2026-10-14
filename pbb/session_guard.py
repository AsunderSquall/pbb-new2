"""Observe Windows session-ending messages without initiating logout/shutdown."""
import os


class SessionGuard:
    """Subclass the Tk outer window; restore its procedure before destruction."""
    def __init__(self, window, save):
        self.hwnd = None
        if os.name != 'nt':
            return
        import ctypes
        from ctypes import wintypes
        self.api = ctypes.WinDLL('user32', use_last_error=True)
        self.api.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.api.GetAncestor.restype = wintypes.HWND
        self.api.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND,
                                           wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.api.CallWindowProcW.restype = ctypes.c_ssize_t
        setter = self.api.SetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else self.api.SetWindowLongW
        setter.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        setter.restype = ctypes.c_void_p
        self.setter = setter
        window.update_idletasks()
        hwnd = self.api.GetAncestor(window.winfo_id(), 2)  # GA_ROOT (Tk's outer window)
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND,
                                          wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        def dispatch(handle, message, wparam, lparam):
            if message in (0x11, 0x16):  # WM_QUERYENDSESSION / WM_ENDSESSION
                if message == 0x11 or wparam:
                    try:
                        save()
                    except Exception:
                        pass  # Saving must never veto a system shutdown.
                return 1 if message == 0x11 else 0
            return self.api.CallWindowProcW(self.previous, handle, message, wparam, lparam)

        self.callback = callback_type(dispatch)  # Keep alive until the hook is removed.
        self.previous = setter(hwnd, -4, ctypes.cast(self.callback, ctypes.c_void_p))
        if not self.previous:
            raise ctypes.WinError(ctypes.get_last_error())
        self.hwnd = hwnd

    def close(self):
        """Remove the hook while the Tk window still exists."""
        if self.hwnd is not None:
            self.setter(self.hwnd, -4, self.previous)
            self.hwnd = None
