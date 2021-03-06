from __future__ import print_function
import sys, os, traceback
from functools import wraps
import binaryninja as bn
from binaryninja import core as bnc
import sip
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Q_ARG, Q_RETURN_ARG
from ctypes import CDLL, CFUNCTYPE, POINTER as CPOINTER
from ctypes import byref as c_byref, cast as c_cast, sizeof as c_sizeof
from ctypes import c_int, c_void_p, c_char_p, c_int64

from ._selfsym import resolve_symbol


try:
    for _on_reload_fn in _on_reload:
        _on_reload_fn()
except NameError:
    pass
_on_reload = []


def on_main_thread(func):
    """Wrap `func` to synchronously execute on the main thread."""

    # or the arguments would get replaced with *args, **kwargs
    if os.getenv('READTHEDOCS'): return func

    @wraps(func)
    def wrapper(*args, **kwargs):
        cell = [None] # no `nonlocal`
        def exn_wrapper():
            try:
                cell[0] = (True, func(*args, **kwargs))
            except Exception:
                bn.log.log_error(traceback.format_exc())
                cell[0] = (False, sys.exc_value)

        bn.mainthread.execute_on_main_thread_and_wait(exn_wrapper)

        is_ok, result = cell[0]
        if is_ok:
            return result
        else:
            print("An exception has occurred while running a function on the main thread.\n"
                  "See the log window for the rest of the backtrace.",
                  file=sys.stderr)
            raise result
    return wrapper


def _q_meta_object_for_class(name):
    return sip.wrapinstance(resolve_symbol('_ZN{}{}16staticMetaObjectE'
                                           .format(len(name), name)),
                            QtCore.QMetaObject)


class _CStaticMethodProxy(object):
    def __init__(self, func_name, func_sig):
        self._func_name = func_name
        self._func_sig = func_sig
        self._func = None

    def __call__(self, *args):
        if self._func is None:
            func_addr = resolve_symbol(self._func_name)
            if func_addr is None:
                raise AttributeError("Symbol {} is not defined".format(self._func_name))
            self._func = self._func_sig(func_addr)

        return self._func(*args)


class _CMethodProxy(object):
    def __init__(self, func, this_ptr):
        self._func = func
        self._this_ptr = this_ptr

    def __call__(self, *args):
        return self._func(self._this_ptr, *args)


class _CObjectProxy(object):
    _c_funcptrs = {}

    def __init__(self, c_ptr, c_api):
        self._c_ptr = c_ptr
        self._c_api = c_api

    def __getattr__(self, attr):
        if attr in self._c_api:
            func_name, func_sig = self._c_api[attr]
            if func_name not in self._c_funcptrs:
                func_addr = resolve_symbol(func_name)
                if func_addr is None:
                    raise AttributeError("Symbol {} is not defined".format(func_name))
                self._c_funcptrs[func_name] = func_addr
            func_addr = self._c_funcptrs[func_name]

            proxy = _CMethodProxy(func_sig(func_addr), self._c_ptr)
            setattr(self, attr, proxy)
            return proxy
        else:
            raise AttributeError("undefined method '{}'".format(attr))

    def _pointer(self):
        return sip.unwrapinstance(self._c_ptr)


class _QMethodProxy(object):
    def __init__(self, q_meta_object, q_self, name):
        self._q_meta_object = q_meta_object
        self._q_self = q_self
        self.name = name

    def __call__(self, *args):
        self._q_meta_object.invokeMethod(self._q_self, self.name, *args)


class _QObjectProxy(_CObjectProxy):
    _q_methods = {}
    _q_properties = {}

    def __init__(self, q_meta_object, q_object, c_api={}):
        _CObjectProxy.__init__(self, sip.unwrapinstance(q_object), c_api)
        self._q_meta_object = q_meta_object
        self._q_object = q_object

        if self._q_meta_object != q_object.metaObject():
            raise TypeError("proxy for '{}' cannot be initialized from a pointer to '{}'"
                            .format(q_meta_object.className(),
                                    q_object.metaObject().className()))

        if self._q_meta_object not in self._q_methods:
            self._q_methods[self._q_meta_object] = \
                [str(self._q_meta_object.method(n).name())
                 for n in range(self._q_meta_object.methodCount())]
        if self._q_meta_object not in self._q_properties:
            self._q_properties[self._q_meta_object] = \
                [str(self._q_meta_object.property(n).name())
                 for n in range(self._q_meta_object.propertyCount())]

    def __getattr__(self, attr):
        if attr in self._q_methods[self._q_meta_object]:
            proxy = _QMethodProxy(self._q_meta_object, self._q_object, attr)
            setattr(self, attr, proxy)
            return proxy
        elif attr in self._c_api:
            return _CObjectProxy.__getattr__(self, attr)
        else:
            return getattr(self._q_object, attr)

    def _className(self):
        return self._q_meta_object.className()

    def _methods(self):
        return self._q_methods[self._q_meta_object]

    def _properties(self):
        return self._q_properties[self._q_meta_object]

    def _all_children(self):
        children = []
        def find_all(widget):
            for child in widget.children():
                children.append(child)
                find_all(child)
        find_all(self._q_object)
        return children


_new          = _CStaticMethodProxy('_Znwm',
                                    CFUNCTYPE(c_void_p, c_int))
_delete       = _CStaticMethodProxy('_ZdlPv',
                                    CFUNCTYPE(None, c_void_p))


# PyQt5 doesn't provide QString anymore, so we have to bind it ourselves.
class _QString(object):
    _c_api = {
        'QString':    ('_ZN7QStringC2EPKc', CFUNCTYPE(None, c_void_p, c_char_p)),
        '_d_QString': ('_ZN7QStringD2Ev',   CFUNCTYPE(None, c_void_p)),
    }

    def __init__(self, value=None):
        self._owned = False
        if isinstance(value, c_void_p):
            self._c = _CObjectProxy(value, self._c_api)
        elif value is None or isinstance(value, str):
            _c_ptr = _new(c_sizeof(c_void_p))
            self._c = _CObjectProxy(_c_ptr, self._c_api)
            self._c.QString(value)
            self._owned = True
        else:
            raise TypeError("QString can only be initialized with None, str, or a pointer")

    def __del__(self):
        if self._owned:
            self._c._d_QString()
            _delete(self._pointer())

    def _pointer(self):
        return self._c._c_ptr


class MainWindow(object):
    """
    Main Binary Ninja window.

    :ivar q: underlying Qt widget proxy
    """

    _q_meta_object = _q_meta_object_for_class('MainWindow')

    _c_static_api = {
        'getActiveWindow': _CStaticMethodProxy('_ZN10MainWindow15getActiveWindowEv',
                                               CFUNCTYPE(c_void_p))
    }

    _c_api = {
        'openFilename':         ('_ZN10MainWindow12openFilenameERK7QString',
                                 CFUNCTYPE(c_int, c_void_p, c_void_p)),
        'openUrl':              ('_ZN10MainWindow7openUrlERK4QUrl',
                                 CFUNCTYPE(c_int, c_void_p, c_void_p)),
        'getCurrentView':       ('_ZN10MainWindow14getCurrentViewEv',
                                 CFUNCTYPE(c_void_p, c_void_p))
    }

    try:
        _init_callbacks = MainWindow._init_callbacks
        _init_set = ViewFrame._init_set
    except NameError:
        _init_callbacks = []
        _init_set = set()

    @classmethod
    def getActiveWindow(cls):
        """
        :return: the active main window
        :rtype: :class:`MainWindow`
        """
        return cls(sip.wrapinstance(cls._c_static_api['getActiveWindow'](),
                                    QtWidgets.QMainWindow))

    @classmethod
    def addInitCallback(cls, fn):
        """
        Registers ``fn`` to be called each time a new main window is opened.

        :param fn: callback function
        :type fn: function(:class:`MainWindow`)
        """
        cls._init_callbacks.append(fn)

    @classmethod
    def removeInitCallback(cls, fn):
        """Unregisters ``fn``."""
        cls._init_callbacks.remove(fn)

    def __init__(self, q_main_window):
        self.q = _QObjectProxy(self._q_meta_object, q_main_window, self._c_api)

    def newWindow(self):
        """Opens a new window."""
        self.q.newWindow()

    def newTab(self):
        """Opens a new tab."""
        self.q.newTab()

    def newBinary(self):
        """Opens a new tab with a new binary file."""
        self.q.newBinary()

    def nextTab(self):
        """Switches to next tab."""
        self.q.nextTab()

    def previousTab(self):
        """Switches to previous tab."""
        self.q.previousTab()

    def newWindowForTab(self):
        """Extracts the current tab into a new window."""
        self.q.newWindowForTab()

    def splitToNewTab(self):
        """Splits the current view into a new tab."""
        self.q.splitToNewTab()

    def splitToNewWindow(self):
        """Splits the current view into a new window."""
        self.q.splitToNewWindow()

    def closeTab(self):
        """Closes the current tab."""
        self.q.closeTab()

    def closeAll(self):
        """Closes all tabs."""
        self.q.closeTab()

    def navigateBack(self):
        """Navigates back in history."""
        self.q.navigateBack(Q_ARG(bool, False))

    def navigateForward(self):
        """Navigates forward in history."""
        self.q.navigateForward(Q_ARG(bool, False))

    def open(self):
        """Opens the file open dialog."""
        self.q.open()

    @on_main_thread
    def openFilename(self, filename):
        """Opens the given filename in a new tab."""
        q_filename = _QString(filename)
        self.q.openFilename(q_filename._pointer())

    def openUrlDialog(self):
        """Opens the URL open dialog."""
        self.q.openUrlDialog()

    @on_main_thread
    def openUrl(self, url):
        """Opens the given URL in a new tab."""
        q_url = QtCore.QUrl(url)
        self.q.openUrl(sip.unwrapinstance(q_url))

    def save(self):
        """Saves the database."""
        self.q.saveDatabase()

    def saveAs(self):
        """Opens the binary contents save dialog."""
        self.q.saveAs()

    def getCurrentView(self):
        """
        :return: view frame for the currently active tab
        :rtype: :class:`ViewFrame`
        """
        p_view_frame = self.q.getCurrentView()
        if p_view_frame:
            q_view_frame = sip.wrapinstance(p_view_frame, QtWidgets.QWidget)
            return ViewFrame(q_view_frame)


class ViewFrame(object):
    """
    A view frame, that is, the info panel and the main view bound to a particular
    binary view.

    :ivar q: underlying Qt widget proxy
    """

    _q_meta_object = _q_meta_object_for_class('ViewFrame')

    _c_api = {
        'back':                 ('_ZN9ViewFrame4backEv',
                                 CFUNCTYPE(None, c_void_p)),
        'forward':              ('_ZN9ViewFrame7forwardEv',
                                 CFUNCTYPE(None, c_void_p)),
        'setViewType':          ('_ZN9ViewFrame11setViewTypeERK7QString',
                                 CFUNCTYPE(c_int, c_void_p, c_void_p)),
        'getCurrentView':       ('_ZN9ViewFrame14getCurrentViewEv',
                                 CFUNCTYPE(c_void_p, c_void_p, c_void_p)),
    }

    try:
        _init_callbacks = ViewFrame._init_callbacks
        _init_set = ViewFrame._init_set
    except NameError:
        _init_callbacks = []
        _init_set = set()

    @classmethod
    def addInitCallback(cls, fn):
        """
        Registers ``fn`` to be called each time a new view frame (i.e. a tab) is opened.

        :param fn: callback function
        :type fn: function(:class:`ViewFrame`)
        """
        cls._init_callbacks.append(fn)

    @classmethod
    def removeInitCallback(cls, fn):
        """Unregisters ``fn``."""
        cls._init_callbacks.remove(fn)

    def __init__(self, q):
        self.q = _QObjectProxy(self._q_meta_object, q, self._c_api)

    @on_main_thread
    def back(self):
        """Navigates back in history."""
        self.q.back()

    @on_main_thread
    def forward(self):
        """Navigates forward in history."""
        self.q.forward()

    @on_main_thread
    def setViewType(self, binary_view_type, disasm_view_type):
        """
        Sets the type of binary view and type of disassembly view.

        :param binary_view_type:
            registered binary view type, e.g. ``"ELF"``
        :param disasm_view_type:
            pre-existing types are ``"Hex"``, ``"Graph"``, ``"Linear"``, ``"Strings"``,
            and ``"Types"``
        :return: ``True`` if successful, ``False`` otherwise
        """
        q_ident = _QString(binary_view_type + ":" + disasm_view_type)
        return self.q.setViewType(q_ident._pointer()) != 0

    def getInfoPanel(self):
        """
        :return: the info panel of this view frame
        :rtype: :class:`InfoPanel`
        """
        for child in self.q._all_children():
            if child.metaObject() == InfoPanel._q_meta_object:
                return InfoPanel(child)
        return None

    def getView(self):
        """
        :return: the main view widget of this view frame
        :rtype: :class:`HexEditor`, :class:`DisassemblyView`, :class:`StringsView`,
            :class:`LinearView`, :class:`TypeView`, or an user-defined subclass.
        """
        for child in self.q._all_children():
            if isinstance(child, QtWidgets.QWidget) and child.isVisible():
                view = View.getViewFromWidget(child)
                if view is not None:
                    return view


class InfoPanel(object):
    """
    An info panel of a view frame.

    :ivar q: underlying Qt widget proxy
    """

    _q_meta_object = _q_meta_object_for_class('InfoPanel')

    def __init__(self, q):
        self.q = _QObjectProxy(self._q_meta_object, q)

    def getTabWidget(self):
        """
        :return: the tab widget of this info panel
        :rtype: ``QtWidgets.QTabWidget``
        """
        for child in self.q._all_children():
            if child.metaObject() == QtWidgets.QTabWidget.staticMetaObject:
                return child


_bn_new_ref_fns = {}

def _from_bn_smart_ptr(ptr, c_type, new_ref):
    if new_ref not in _bn_new_ref_fns:
        _bn_new_ref_fns[new_ref] = _CStaticMethodProxy(new_ref, CFUNCTYPE(c_void_p, c_void_p))
    new_ref_fn = _bn_new_ref_fns[new_ref]

    # The layout of class CoreRefCountObject is as follows:
    #   void* vtbl;
    #   int   m_refs;
    #   T*    m_object;
    # We need m_object.
    c_object = c_cast(ptr + c_sizeof(c_void_p) * 2, CPOINTER(c_void_p)).contents
    return bnc.handle_of_type(c_cast(new_ref_fn(c_object), c_void_p), c_type)

def _binary_view_from_cxx_ref(ptr):
    return bn.BinaryView(handle=_from_bn_smart_ptr(ptr,
                bnc.BNBinaryView, 'BNNewViewReference'))


class View(object):
    """
    The base class of all views.

    :ivar q: underlying Qt widget proxy
    """

    @classmethod
    def getViewFromWidget(cls, q_widget):
        for subcls in cls.__subclasses__():
            if q_widget.metaObject() == subcls._q_meta_object:
                return subcls(q_widget)

    def __init__(self, q):
        self.q = _QObjectProxy(self._q_meta_object, q, self._c_api)


class HexEditor(View):
    """
    A hex editor view.

    :ivar q: underlying Qt widget proxy
    """

    _q_meta_object = _q_meta_object_for_class('HexEditor')

    _c_api = {
        'getData':          ('_ZN9HexEditor7getDataEv',
                             CFUNCTYPE(c_void_p, c_void_p)),
    }

    def getBinaryView(self):
        """
        :return: the binary view of this view
        """
        return _binary_view_from_cxx_ref(self.q.getData())


class DisassemblyView(View):
    """
    A graph disassembly view.

    :ivar q: underlying Qt widget proxy
    """

    _q_meta_object = _q_meta_object_for_class('DisassemblyView')

    _c_api = {
        'getData':          ('_ZN15DisassemblyView7getDataEv',
                             CFUNCTYPE(c_void_p, c_void_p)),
    }

    def getBinaryView(self):
        """
        :return: the binary view of this view
        """
        return _binary_view_from_cxx_ref(self.q.getData())


class StringsView(View):
    """
    A strings view.

    :ivar q: underlying Qt widget proxy
    """

    _q_meta_object = _q_meta_object_for_class('StringsView')

    _c_api = {
        'getData':          ('_ZN11StringsView7getDataEv',
                             CFUNCTYPE(c_void_p, c_void_p)),
        'navigate':         ('_ZN11StringsView8navigateEm',
                             CFUNCTYPE(c_int, c_void_p, c_int64))
    }

    def getBinaryView(self):
        """
        :return: the binary view of this view
        """
        return _binary_view_from_cxx_ref(self.q.getData())

    def navigate(self, addr):
        """
        :param addr: address of the string to highlight
        :type addr: int
        :return: ``True`` if successful, ``False`` otherwise
        """
        return self.q.navigate(addr) != 0


class LinearView(View):
    """
    A linear disassembly view.

    :ivar q: underlying Qt widget proxy
    """

    _q_meta_object = _q_meta_object_for_class('LinearView')

    _c_api = {
        'getData':          ('_ZN10LinearView7getDataEv',
                             CFUNCTYPE(c_void_p, c_void_p)),
    }

    def getBinaryView(self):
        """
        :return: the binary view of this view
        """
        return _binary_view_from_cxx_ref(self.q.getData())


class TypeView(View):
    """
    A type view.

    :ivar q: underlying Qt widget proxy
    """

    _q_meta_object = _q_meta_object_for_class('TypeView')

    _c_api = {
        'getData':          ('_ZN10TypeView7getDataEv',
                             CFUNCTYPE(c_void_p, c_void_p)),
    }

    def getBinaryView(self):
        """
        :return: the binary view of this view
        """
        return _binary_view_from_cxx_ref(self.q.getData())


class CrossReferenceItemDelegate(object):
    """
    An item delegate used to paint the cross references window.

    This delegate expects the display role of the cell to contain data in
    a simple hierarchical format:

    ::
        color = 0xffffff
        text  = "some text"
        word  = [color, text]
        line  = [word, word, ...]
        lines = [line, line, ...]

    :ivar q: underlying Qt widget proxy
    """

    _q_meta_object = _q_meta_object_for_class('CrossReferenceItemDelegate')

    _c_api = {
        '_constructor':     ('_ZN26CrossReferenceItemDelegateC1EP7QWidget',
                             CFUNCTYPE(c_void_p, c_void_p)),
        '_destructor':      ('_ZN26CrossReferenceItemDelegateD1Ev',
                             CFUNCTYPE(c_void_p)),
        'updateFonts':      ('_ZN26CrossReferenceItemDelegate11updateFontsEv',
                             CFUNCTYPE(None, c_void_p)),
    }

    def __init__(self, parent=None):
        c_ptr = _new(0x30)
        if parent:
            if not isinstance(parent.q._q_object, QtCore.QObject):
                raise TypeError("parent must be None or an object")
            _CObjectProxy(c_ptr, self._c_api)._constructor(parent.q._c_ptr)
        else:
            _CObjectProxy(c_ptr, self._c_api)._constructor(None)
        q_object = sip.wrapinstance(c_ptr, QtCore.QObject)
        self.q = _QObjectProxy(self._q_meta_object, q_object, self._c_api)


def getActiveWindow():
    """Returns the focused main window. See :meth:`MainWindow.getActiveWindow`."""
    return MainWindow.getActiveWindow()


_getThemeColor = _CStaticMethodProxy('_Z13getThemeColor10ThemeColor',
                                     CFUNCTYPE(c_int, c_void_p, c_int))

def getThemeColor(name):
    """
    Returns the ``QColor`` corresponding to the symbolic theme color.
    :param name: one of ``address``, ``symbol``, or an integer
    """
    if name == 'address':
        name = 0
    if name == 'symbol':
        name = 0x19

    q_color = QtGui.QColor()
    _getThemeColor(sip.unwrapinstance(q_color), name)
    return q_color


class _ApplicationEventFilter(QtCore.QObject):
    def __init__(self):
        QtCore.QObject.__init__(self)
        q_app = QtWidgets.QApplication.instance()
        q_app.installEventFilter(self)
        _on_reload.append(lambda: q_app.removeEventFilter(self))

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Show:
            for cls in [MainWindow, ViewFrame]:
                if watched.metaObject() == cls._q_meta_object and watched not in cls._init_set:
                    cls._init_set.add(watched)
                    def cleanup():
                        if watched in cls._init_set:
                            cls._init_set.remove(watched)
                    watched.destroyed.connect(cleanup)

                    obj = cls(watched)
                    for callback in cls._init_callbacks:
                        try:
                            callback(obj)
                        except:
                            bn.log.log_error(traceback.format_exc())
        return False

bn.mainthread.execute_on_main_thread_and_wait(lambda: _ApplicationEventFilter())
