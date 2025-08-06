import base64
import contextlib
import enum
import functools
import io
import logging
import os
import re
import threading
import time
from collections import defaultdict, namedtuple
from typing import Optional, Union, Callable, Dict, NamedTuple
from urllib.parse import urlparse, urljoin
import requests

import retry

from wda import exceptions
from wda.utils import limit_call_depth, AttrDict, convert

try:
    import sys

    import logzero
    if not (hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()):
        log_format = '[%(levelname)1.1s %(asctime)s %(module)s:%(lineno)d] %(message)s'
        logzero.setup_default_logger(formatter=logzero.LogFormatter(
            fmt=log_format))
    logger = logzero.logger
except ImportError:
    logger = logging.getLogger("facebook-wda")  # default level: WARNING

DEBUG = False
HTTP_TIMEOUT = 180.0  # unit second
DEVICE_WAIT_TIMEOUT = 180.0  # wait ready

LANDSCAPE = 'LANDSCAPE'
PORTRAIT = 'PORTRAIT'
LANDSCAPE_RIGHT = 'UIA_DEVICE_ORIENTATION_LANDSCAPERIGHT'
PORTRAIT_UPSIDEDOWN = 'UIA_DEVICE_ORIENTATION_PORTRAIT_UPSIDEDOWN'

ELEMENTS = [
    'Any',
    'Other',
    'Application',
    'Group',
    'Window',
    'Sheet',
    'Drawer',
    'Alert',
    'Dialog',
    'Button',
    'RadioButton',
    'RadioGroup',
    'CheckBox',
    'DisclosureTriangle',
    'PopUpButton',
    'ComboBox',
    'MenuButton',
    'ToolbarButton',
    'Popover',
    'Keyboard',
    'Key',
    'NavigationBar',
    'TabBar',
    'TabGroup',
    'Toolbar',
    'StatusBar',
    'Table',
    'TableRow',
    'TableColumn',
    'Outline',
    'OutlineRow',
    'Browser',
    'CollectionView',
    'Slider',
    'PageIndicator',
    'ProgressIndicator',
    'ActivityIndicator',
    'SegmentedControl',
    'Picker',
    'PickerWheel',
    'Switch',
    'Toggle',
    'Link',
    'Image',
    'Icon',
    'SearchField',
    'ScrollView',
    'ScrollBar',
    'StaticText',
    'TextField',
    'SecureTextField',
    'DatePicker',
    'TextView',
    'Menu',
    'MenuItem',
    'MenuBar',
    'MenuBarItem',
    'Map',
    'WebView',
    'IncrementArrow',
    'DecrementArrow',
    'Timeline',
    'RatingIndicator',
    'ValueIndicator',
    'SplitGroup',
    'Splitter',
    'RelevanceIndicator',
    'ColorWell',
    'HelpTag',
    'Matte',
    'DockItem',
    'Ruler',
    'RulerMarker',
    'Grid',
    'LevelIndicator',
    'Cell',
    'LayoutArea',
    'LayoutItem',
    'Handle',
    'Stepper',
    'Tab'
]


# default_alert_accept_selector = "**/XCUIElementTypeButton[`label IN {'允许','好','仅在使用应用期间','暂不'}`]"
# default_alert_dismiss_selector = "**/XCUIElementTypeButton[`label IN {'不允许','暂不'}`]"


class AlertAction(str, enum.Enum):
    ACCEPT = "accept"
    DISMISS = "dismiss"


class Status(enum.IntEnum):
    # 不是怎么准确，status在mds平台上变来变去的
    UNKNOWN = 100  # other status
    ERROR = 110


class Callback(str, enum.Enum):
    ERROR = "::error"
    HTTP_REQUEST_BEFORE = "::http-request-before"
    HTTP_REQUEST_AFTER = "::http-request-after"

    RET_RETRY = "::retry"  # Callback return value
    RET_ABORT = "::abort"
    RET_CONTINUE = "::continue"

    # Old implement
    # return namedtuple('GenericDict', list(dictionary.keys()))(**dictionary)


def roundint(i):
    return int(round(i, 0))

class HTTPRequest(NamedTuple):
    fetch: Callable[..., AttrDict]
    get: Callable[[str, Optional[Dict], Optional[float]], AttrDict]
    post: Callable[[str, Optional[Dict], Optional[float]], AttrDict]

class HTTPSessionRequest(NamedTuple):
    fetch: Callable[..., AttrDict]
    get: Callable[[str, Optional[Dict], Optional[float]], AttrDict]
    post: Callable[[str, Optional[Dict], Optional[float]], AttrDict]
    delete: Callable[[str, Optional[Dict], Optional[float]], AttrDict]

class BaseClient():
    def __init__(self, url=None, _session_id=None):
        """
        Args:
            target (string): the device url

        If target is empty, device url will set to env-var "DEVICE_URL" if defined else set to "http://localhost:8100"
        """
        if not url.endswith("/"):
            url += '/'
        parsed = urlparse(url)
        # Session variable
        self.__wda_url = parsed.geturl()
        self.__session_id = _session_id
        self.__timeout = 30.0
        self.__callbacks = defaultdict(list)

    def is_ready(self) -> bool:
        try:
            self.http.get("status", timeout=3)
            return True
        except Exception as e:
            return False

    def wait_ready(self, timeout=120, noprint=False) -> bool:
        """
        wait until WDA back to normal

        Returns:
            bool (if wda works)
        """
        deadline = time.time() + timeout

        def _dprint(message: str):
            if noprint:
                return
            print("facebook-wda", time.ctime(), message)

        _dprint("Wait ready (timeout={:.1f})".format(timeout))
        while time.time() < deadline:
            if self.is_ready():
                _dprint("device back online")
                return True
            else:
                _dprint("{!r} wait_ready left {:.1f} seconds".format(self.__wda_url, deadline - time.time()))
                time.sleep(1.0)
        _dprint("device still offline")
        return False

    @retry.retry(exceptions=exceptions.WDAEmptyResponseError, tries=3, delay=2)
    def status(self):
        res = self.http.get('status')
        res["value"]['sessionId'] = res.get("sessionId")
        # Can't use res.value['sessionId'] = ...
        return res.value
    
    def _fetch(self,
               method: str,
               urlpath: str,
               data: Optional[dict] = None,
               with_session: bool = False,
               timeout: Optional[float] = None) -> AttrDict:
        """ do http request """

        if with_session:
            url = urljoin(self.__wda_url, f"session/{self.session_id}/")
            url = urljoin(url, urlpath)
        else:
            url = urljoin(self.__wda_url, urlpath)

        # 构造 headers
        headers = {"Content-Type": "application/json"} if data else {}

        # 构造 json 参数
        json_data = data if data else None

        try:
            resp = requests.request(
                method.upper(),
                url,
                json=json_data,
                headers=headers,
                timeout=timeout
            )
        except requests.RequestException as e:
            # 网络层异常统一转成 WDAError
            raise exceptions.WDAError(method, url, str(e))

        # 502 Bad Gateway
        if resp.status_code == 502:
            raise exceptions.WDABadGateway(resp.status_code, resp.text)

        # 空 body
        if not resp.text.strip():
            raise exceptions.WDAEmptyResponseError(method, url, data)

        # 解析 JSON
        try:
            retjson = resp.json()
        except ValueError:
            raise exceptions.WDAError(method, url, resp.text[:100] + "...")

        retjson.setdefault("status", 0)
        r = convert(retjson)

        # 处理 WDA 返回的错误
        if isinstance(r.value, dict) and r.value.get("error"):
            status = Status.ERROR
            value = r.value.copy()
            value.pop("traceback", None)

            for err_cls in (
                exceptions.WDAInvalidSessionIdError,
                exceptions.WDAPossiblyCrashedError,
                exceptions.WDAKeyboardNotPresentError,
                exceptions.WDAStaleElementReferenceError,
                exceptions.WDAUnknownError,
            ):
                if err_cls.check(value):
                    raise err_cls(status, value)

            raise exceptions.WDARequestError(status, value)

        return r

    @property
    def http(self) -> HTTPRequest:
        return HTTPRequest(
            self._fetch,
            functools.partial(self._fetch, "GET"),
            functools.partial(self._fetch, "POST"))  # yapf: disable

    @property
    def _session_http(self) -> HTTPSessionRequest:
        return HTTPSessionRequest(
            functools.partial(self._fetch, with_session=True),
            functools.partial(self._fetch, "GET", with_session=True),
            functools.partial(self._fetch, "POST", with_session=True),
            functools.partial(self._fetch, "DELETE", with_session=True))  # yapf: disable

    def home(self) -> Optional[AttrDict]:
        """Press home button"""
        try:
            self.http.post('wda/homescreen')
        except exceptions.WDARequestError as e:
            if "Timeout waiting until SpringBoard is visible" in str(e):
                return
            raise

    def healthcheck(self):
        """Hit healthcheck"""
        return self.http.get('wda/healthcheck')

    def locked(self) -> bool:
        """ returns locked status, true or false """
        return self.http.get("wda/locked").value

    def lock(self):
        return self.http.post('wda/lock')

    def unlock(self):
        """ unlock screen, double press home """
        return self.http.post('wda/unlock')

    def sleep(self, secs: float):
        """ same as time.sleep """
        time.sleep(secs)

    @retry.retry(exceptions.WDAUnknownError, tries=3, delay=.5, jitter=.2)
    def app_current(self) -> dict:
        """
        Returns:
            dict, eg:
            {"pid": 1281,
             "name": "",
             "bundleId": "com.netease.cloudmusic"}
        """
        return self.http.get("wda/activeAppInfo").value

    def source(self, format='xml', accessible=False):
        """
        Args:
            format (str): only 'xml' and 'json' source types are supported
            accessible (bool): when set to true, format is always 'json'
        """
        if accessible:
            return self.http.get('wda/accessibleSource').value
        return self.http.get('source?format=' + format).value

    def screenshot(self, png_filename=None, format='pillow'):
        """
        Screenshot with PNG format

        Args:
            png_filename(string): optional, save file name
            format(string): return format, "raw" or "pillow” (default)

        Returns:
            PIL.Image or raw png data

        Raises:
            WDARequestError
        """
        value = self.http.get('screenshot').value
        raw_value = base64.b64decode(value)
        png_header = b"\x89PNG\r\n\x1a\n"
        if not raw_value.startswith(png_header) and png_filename:
            raise exceptions.WDARequestError(-1, "screenshot png format error")

        if png_filename:
            with open(png_filename, 'wb') as f:
                f.write(raw_value)

        if format == 'raw':
            return raw_value
        elif format == 'pillow':
            from PIL import Image
            buff = io.BytesIO(raw_value)
            im = Image.open(buff)
            return im.convert("RGB") # convert to RGB to fix save jpeg error
        else:
            raise ValueError("unknown format")

    def session(self,
                bundle_id=None,
                arguments: Optional[list] = None,
                environment: Optional[dict] = None,
                alert_action: Optional[AlertAction] = None):
        """
        Launch app in a session

        Args:
            - bundle_id (str): the app bundle id
            - arguments (list): ['-u', 'https://www.google.com/ncr']
            - enviroment (dict): {"KEY": "VAL"}
            - alert_action (AlertAction): AlertAction.ACCEPT or AlertAction.DISMISS

        WDA Return json like

        {
            "value": {
                "sessionId": "69E6FDBA-8D59-4349-B7DE-A9CA41A97814",
                "capabilities": {
                    "device": "iphone",
                    "browserName": "部落冲突",
                    "sdkVersion": "9.3.2",
                    "CFBundleIdentifier": "com.supercell.magic"
                }
            },
            "sessionId": "69E6FDBA-8D59-4349-B7DE-A9CA41A97814",
            "status": 0
        }

        To create a new session, send json data like

        {
            "capabilities": {
                "alwaysMatch": {
                    "bundleId": "your-bundle-id",
                    "app": "your-app-path"
                    "shouldUseCompactResponses": (bool),
                    "shouldUseTestManagerForVisibilityDetection": (bool),
                    "maxTypingFrequency": (integer),
                    "arguments": (list(str)),
                    "environment": (dict: str->str)
                }
            },
        }

        Or {"capabilities": {}}
        """
        # if not bundle_id:
        #     # 旧版的WDA创建Session不允许bundleId为空，但是总是可以拿到sessionId
        #     # 新版的WDA允许bundleId为空，但是初始状态没有sessionId
        #     session_id = self.status().get("sessionId")
        #     if session_id:
        #         return self

        capabilities = {}
        if bundle_id:
            always_match = {
                "bundleId": bundle_id,
                "arguments": arguments or [],
                "environment": environment or {},
                "shouldWaitForQuiescence": False,
            }
            if alert_action:
                assert alert_action in ["accept", "dismiss"]
                capabilities["defaultAlertAction"] = alert_action

            capabilities['alwaysMatch'] = always_match

        payload = {
            "capabilities": capabilities,
            "desiredCapabilities": capabilities.get('alwaysMatch',
                                                    {}),  # 兼容旧版的wda
        }

        # when device is Locked, it is unable to start app
        if self.locked():
            self.unlock()
        try:
            res = self.http.post('session', payload)
        except exceptions.WDAEmptyResponseError:
            """ when there is alert, might be got empty response
            use /wda/apps/state may still get sessionId
            """
            res = self.session().app_state(bundle_id)
            if res.value != 4:
                raise
        client = Client(self.__wda_url, _session_id=res.sessionId)
        client.__timeout = self.__timeout
        client.__callbacks = self.__callbacks
        return client


    '''
    TODO: Should the ctx of the client be written back after this code is executed,\
    as the session ID is already empty when delete session api trigger.
    '''
    def close(self):
        '''Close created session which session id saved in class ctx.'''
        try:
            return self._session_http.delete('')
        except exceptions.WDARequestError as e:
            if not isinstance(e, (exceptions.WDAInvalidSessionIdError, exceptions.WDAPossiblyCrashedError)):
                raise

    #@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@#
    ######  Session methods and properties ######
    #@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@#
    def __enter__(self):
        """
        Usage example:
            with c.session("com.example.app") as app:
                # do something
        """
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    @property
    def session_id(self) -> str:
        if self.__session_id:
            return self.__session_id
        current_sid = self.status()['sessionId']
        if current_sid:
            self.__session_id = current_sid  # store old session id to reduce request count
            return current_sid
        return self.session().session_id

    @session_id.setter
    def session_id(self, value):
        self.__session_id = value

    def _get_session_id(self) -> str:
        return self.session_id

    @functools.cached_property
    def scale(self) -> int:
        """
        UIKit scale factor

        Refs:
            https://developer.apple.com/library/archive/documentation/DeviceInformation/Reference/iOSDeviceCompatibility/Displays/Displays.html
        There is another way to get scale
            self._session_http.get("/wda/screen").value returns {"statusBarSize": {'width': 320, 'height': 20}, 'scale': 2}
        """
        try:
            return self._session_http.get("wda/screen").value['scale']
        except (KeyError, exceptions.WDARequestError):
            v = max(self.screenshot().size) / max(self.window_size())
            return round(v)

    @functools.cached_property
    def bundle_id(self):
        """ the session matched bundle id """
        v = self._session_http.get("/").value
        return v['capabilities'].get('CFBundleIdentifier')

    def implicitly_wait(self, seconds):
        """
        set default element search timeout
        """
        assert isinstance(seconds, (int, float))
        self.__timeout = seconds

    def battery_info(self):
        """
        Returns dict: (I do not known what it means)
            eg: {"level": 1, "state": 2}
        """
        return self._session_http.get("wda/batteryInfo").value

    def device_info(self):
        """
        Returns dict:
            eg: {'currentLocale': 'zh_CN', 'timeZone': 'Asia/Shanghai'}
        """
        return self._session_http.get("wda/device/info").value

    @property
    def info(self):
        """
        Returns:
            {'timeZone': 'Asia/Shanghai',
            'currentLocale': 'zh_CN',
            'model': 'iPhone',
            'uuid': '9DAC43B3-6887-428D-B5D5-4892D1F38BAA',
            'userInterfaceIdiom': 0,
            'userInterfaceStyle': 'unsupported',
            'name': 'iPhoneSE',
            'isSimulator': False}
        """
        return self.device_info()

    def set_clipboard(self, content, content_type="plaintext"):
        """ set clipboard """
        self._session_http.post(
            "wda/setPasteboard", {
                "content": base64.b64encode(content.encode()).decode(),
                "contentType": content_type
            })

    def get_clipboard(self):
        """ Get clipboard text.

        If you want to use this function, you have to set wda foreground which would switch the 
        current screen of the phone. Then we will try to switch back to the screen before.

        Args:
            wda_bundle_id: The bundle id of the started wda.

        Returns:
            Clipboard text.
        """
        clipboard_text = self._session_http.post("wda/getPasteboard").value
        return base64.b64decode(clipboard_text).decode('utf-8')
    
    def siri_activate(self, text):
       self._session_http.post("wda/siri/activate", {"text": text})

    def app_launch(self,
                   bundle_id,
                   arguments=[],
                   environment={},
                   wait_for_quiescence=False):
        """
        Args:
            - bundle_id (str): the app bundle id
            - arguments (list): ['-u', 'https://www.google.com/ncr']
            - enviroment (dict): {"KEY": "VAL"}
            - wait_for_quiescence (bool): default False
        """
        # Deprecated, use app_start instead
        assert isinstance(arguments, (tuple, list))
        assert isinstance(environment, dict)

        # When device is locked, it is unable to launch
        if self.locked():
            self.unlock()

        return self._session_http.post(
            "wda/apps/launch", {
                "bundleId": bundle_id,
                "arguments": arguments,
                "environment": environment,
                "shouldWaitForQuiescence": wait_for_quiescence,
            })

    def app_activate(self, bundle_id):
        return self._session_http.post("wda/apps/activate", {
            "bundleId": bundle_id,
        })

    def app_terminate(self, bundle_id):
        # Deprecated, use app_stop instead
        return self._session_http.post("wda/apps/terminate", {
            "bundleId": bundle_id,
        })

    def app_state(self, bundle_id):
        """
        Returns example:
            {
                "value": 4,
                "sessionId": "0363BDC5-4335-47ED-A54E-F7CCB65C6A65"
            }

        value 1(not running) 2(running in background) 3(running in foreground)
        """
        return self._session_http.post("wda/apps/state", {
            "bundleId": bundle_id,
        })

    def app_start(self,
                  bundle_id,
                  arguments=[],
                  environment={},
                  wait_for_quiescence=False):
        """ alias for app_launch """
        return self.app_launch(bundle_id, arguments, environment,
                               wait_for_quiescence)

    def app_stop(self, bundle_id: str):
        """ alias for app_terminate """
        self.app_terminate(bundle_id)

    def app_list(self):
        """
        Not working very well, only show springboard

        Returns:
            list of app

        Return example:
            [{'pid': 52, 'bundleId': 'com.apple.springboard'}]
        """
        return self._session_http.get("wda/apps/list").value

    def open_url(self, url):
        """
        TODO: Never successed using before. Looks like use Siri to search.
        https://github.com/facebook/WebDriverAgent/blob/master/WebDriverAgentLib/Commands/FBSessionCommands.m#L43
        Args:
            url (str): url

        Raises:
            WDARequestError
        """
        if os.getenv("TMQ_ORIGIN") == "civita": # MDS platform
            return self.http.post("/mds/openurl", {"url": url})
        return self._session_http.post('url', {'url': url})

    def deactivate(self, duration):
        """Put app into background and than put it back
        Args:
            - duration (float): deactivate time, seconds
        """
        return self._session_http.post('wda/deactivateApp',
                                       dict(duration=duration))

    def tap(self, x, y):
        # Support WDA `BREAKING CHANGES`
        # More see: https://github.com/appium/WebDriverAgent/blob/master/CHANGELOG.md#600-2024-01-31
        try:
            return self._session_http.post('wda/tap', dict(x=x, y=y))
        except:
            return self._session_http.post('wda/tap/0', dict(x=x, y=y))

    def _percent2pos(self, x, y, window_size=None):
        if any(isinstance(v, float) for v in [x, y]):
            w, h = window_size or self.window_size()
            x = int(x * w) if isinstance(x, float) else x
            y = int(y * h) if isinstance(y, float) else y
            assert w >= x >= 0
            assert h >= y >= 0
        return (x, y)

    def click(self, x, y, duration: Optional[float] = None):
        """
        Combine tap and tap_hold

        Args:
            x, y: can be float(percent) or int
            duration (optional): tap_hold duration
        """
        x, y = self._percent2pos(x, y)
        if duration:
            return self.tap_hold(x, y, duration)
        return self.tap(x, y)

    def double_tap(self, x, y):
        x, y = self._percent2pos(x, y)
        return self._session_http.post('wda/doubleTap', dict(x=x, y=y))

    def tap_hold(self, x, y, duration=1.0):
        """
        Tap and hold for a moment

        Args:
            - x, y(int, float): float(percent) or int(absolute coordicate)
            - duration(float): seconds of hold time

        [[FBRoute POST:@"/wda/touchAndHold"] respondWithTarget:self action:@selector(handleTouchAndHoldCoordinate:)],
        """
        x, y = self._percent2pos(x, y)
        data = {'x': x, 'y': y, 'duration': duration}
        return self._session_http.post('wda/touchAndHold', data=data)

    def swipe(self, x1, y1, x2, y2, duration=0):
        """
        Args:
            x1, y1, x2, y2 (int, float): float(percent), int(coordicate)
            duration (float): start coordinate press duration (seconds)

        [[FBRoute POST:@"/wda/dragfromtoforduration"] respondWithTarget:self action:@selector(handleDragCoordinate:)],
        """
        if any(isinstance(v, float) for v in [x1, y1, x2, y2]):
            size = self.window_size()
            x1, y1 = self._percent2pos(x1, y1, size)
            x2, y2 = self._percent2pos(x2, y2, size)

        data = dict(fromX=x1, fromY=y1, toX=x2, toY=y2, duration=duration)
        return self._session_http.post('wda/dragfromtoforduration', data=data)

    def _fast_swipe(self, x1, y1, x2, y2, velocity: int = 500):
        """
        velocity: the larger the faster
        """
        data = dict(fromX=x1, fromY=y1, toX=x2, toY=y2, velocity=velocity)
        return self._session_http.post('wda/drag', data=data)

    def swipe_left(self):
        """ swipe right to left """
        w, h = self.window_size()
        return self.swipe(w, h // 2, 1, h // 2)

    def swipe_right(self):
        """ swipe left to right """
        w, h = self.window_size()
        return self.swipe(1, h // 2, w, h // 2)

    def swipe_up(self):
        """ swipe from center to top """
        w, h = self.window_size()
        return self.swipe(w // 2, h // 2, w // 2, 1)

    def swipe_down(self):
        """ swipe from center to bottom """
        w, h = self.window_size()
        return self.swipe(w // 2, h // 2, w // 2, h - 1)

    def _fast_swipe_ext(self, direction: str):
        if direction == "up":
            w, h = self.window_size()
            return self.swipe(w // 2, h // 2, w // 2, 1)
        elif direction == "down":
            w, h = self.window_size()
            return self._fast_swipe(w // 2, h // 2, w // 2, h - 1)
        else:
            raise RuntimeError("not supported direction:", direction)

    @property
    def orientation(self):
        """
        Return string
        One of <PORTRAIT | LANDSCAPE>
        """
        for _ in range(3):
            result = self._session_http.get('orientation').value
            if result:
                return result
            time.sleep(.5)

    @orientation.setter
    def orientation(self, value):
        """
        Args:
            - orientation(string): LANDSCAPE | PORTRAIT | UIA_DEVICE_ORIENTATION_LANDSCAPERIGHT |
                    UIA_DEVICE_ORIENTATION_PORTRAIT_UPSIDEDOWN
        """
        return self._session_http.post('orientation',
                                       data={'orientation': value})

    def window_size(self):
        """
        Returns:
            namedtuple: eg
                Size(width=320, height=568)
        """
        size = self._unsafe_window_size()
        if min(size) > 0:
            return size

        # get orientation, handle alert
        _ = self.orientation  # after this operation, may safe to get window_size
        if self.alert.exists:
            self.alert.accept()
            time.sleep(.1)

        size = self._unsafe_window_size()
        if min(size) > 0:
            return size

        logger.warning("unable to get window_size(), try to to create a new session")
        with self.session("com.apple.Preferences") as app:
            size = app._unsafe_window_size()
            assert min(size) > 0, "unable to get window_size"
            return size

    def _unsafe_window_size(self):
        """
        returns (width, height) might be (0, 0)
        """
        value = self._session_http.get('window/size').value
        w = roundint(value['width'])
        h = roundint(value['height'])
        return namedtuple('Size', ['width', 'height'])(w, h)

    @retry.retry(exceptions.WDAKeyboardNotPresentError, tries=3, delay=1.0)
    def send_keys(self, value):
        """
        send keys, yet I know not, todo function
        """
        if isinstance(value, str):
            value = list(value)
        return self._session_http.post('wda/keys', data={'value': value})

    def press(self, name: str):
        """
        Args:
            name: one of <home|volumeUp|volumeDown>
        """
        valid_names = ("home", "volumeUp", "volumeDown")
        if name not in valid_names:
            raise ValueError(
                f"Invalid name: {name}, should be one of {valid_names}")
        self._session_http.post("wda/pressButton", {"name": name})

    def press_duration(self, name: str, duration: float):
        """
        Args:
            name: one of <home|volumeUp|volumeDown|power|snapshot>
            duration: seconds

        Notes:
            snapshot equals power+home

        Raises:
            ValueError

        Refs:
            https://github.com/appium/WebDriverAgent/pull/494/files
        """
        hid_usages = {
            "home": 0x40,
            "volumeup": 0xE9,
            "volumedown": 0xEA,
            "power": 0x30,
            "snapshot": 0x65,
            "power+home": 0x65
        }
        name = name.lower()
        if name not in hid_usages:
            raise ValueError("Invalid name:", name)
        hid_usage = hid_usages[name]
        return self._session_http.post("wda/performIoHidEvent", {"page": 0x0C, "usage": hid_usage, "duration": duration})

    def keyboard_dismiss(self):
        """
        Not working for now
        """
        raise RuntimeError("not pass tests, this method is not allowed to use")
        self._session_http.post('wda/keyboard/dismiss')

    def appium_settings(self, value: Optional[dict] = None) -> dict:
        """
        Get and set /session/$sessionId/appium/settings
        """
        if value is None:
            return self._session_http.get("/appium/settings").value
        return self._session_http.post("/appium/settings",
                                       data={
                                           "settings": value
                                       }).value

    @functools.cached_property
    def alibaba(self):
        """ Only used in alibaba company """
        try:
            import wda_taobao
            return wda_taobao.Alibaba(self)
        except ImportError:
            raise RuntimeError(
                "@alibaba property requires wda_taobao library installed")

    @functools.cached_property
    def taobao(self):
        try:
            import wda_taobao
            return wda_taobao.Taobao(self)
        except ImportError:
            raise RuntimeError(
                "@taobao property requires wda_taobao library installed")


class Alert(object):
    DEFAULT_ACCEPT_BUTTONS = [
        "使用App时允许", "无线局域网与蜂窝网络", "好", "稍后", "稍后提醒", "确定",
        "允许", "以后", "打开", "录屏", "Allow", "OK", "YES", "Yes", "Later", "Close"
    ]

    def __init__(self, client: BaseClient):
        self._c = client
        self.http = client._session_http

    @property
    def exists(self):
        try:
            self.text
            return True
        except exceptions.WDARequestError as e:
            # expect e.status != 27 in old version and e.value == 'no such alert' in new version
            return False

    @property
    def text(self):
        return self.http.get('alert/text').value
    
    def set_text(self, text: str):
        '''Set text to alert.
        Except return example:
            ```
            wda.exceptions.WDARequestError: WDARequestError(status=110, 
            value={'error': 'no such alert', 'message': 'An attempt was 
            made to operate on a modal dialog when one was not open'})```
        '''
        return self.http.post('alert/text', data={'value': text})

    def wait(self, timeout=20.0):
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self.exists:
                return True
            time.sleep(0.2)
        return False

    def accept(self):
        return self.http.post('alert/accept')

    def dismiss(self):
        return self.http.post('alert/dismiss')

    def buttons(self):
        return self.http.get('wda/alert/buttons').value

    def click(self, button_name: Optional[Union[str, list]] = None):
        """
        Args:
            - button_name: the name of the button

        Returns:
            button_name being clicked

        Raises:
            ValueError when button_name is not in avaliable button names
        """
        # Actually, It has no difference POST to accept or dismiss
        if isinstance(button_name, str):
            self.http.post('alert/accept', data={"name": button_name})
            return button_name

        avaliable_names = self.buttons()
        buttons: list = button_name
        for bname in buttons:
            if bname in avaliable_names:
                return self.click(bname)
        raise ValueError("Only these buttons can be clicked", avaliable_names)

    def click_exists(self, buttons: Optional[Union[str, list]] = None):
        """
         Args:
            - buttons: the name of the button of list of names

        Returns:
            button_name clicked or None
        """
        try:
            return self.click(buttons)
        except (ValueError, exceptions.WDARequestError):
            return None

    @contextlib.contextmanager
    def watch_and_click(self,
                        buttons: Optional[list] = None,
                        interval: float = 2.0):
        """ watch and click button
        Args:
            buttons: buttons name which need to click
            interval: check interval
        """
        if not buttons:
            buttons = self.DEFAULT_ACCEPT_BUTTONS

        event = threading.Event()

        def _inner():
            while not event.is_set():
                try:
                    alert_buttons = self.buttons()
                    logger.info("Alert detected, buttons: %s", alert_buttons)
                    for btn_name in buttons:
                        if btn_name in alert_buttons:
                            logger.info("Alert click: %s", btn_name)
                            self.click(btn_name)
                            break
                    else:
                        logger.warning("Alert not handled")
                except exceptions.WDARequestError:
                    pass
                time.sleep(interval)

        threading.Thread(name="alert", target=_inner, daemon=True).start()
        yield None
        event.set()


class Client(BaseClient):
    @property
    def alert(self) -> Alert:
        return Alert(self)
