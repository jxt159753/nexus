"""A persistent virtual phone owned by this Windows Node, never a physical device.

The Android SDK remains a separately installed, pinned local runtime. Userdata is
never wiped, restored from a snapshot or copied from an existing phone. Core's
plugin task/receipt lifecycle authorizes and journals every operation.
"""

import base64
import hashlib
import io
import json
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path
from uuid import uuid4

from nexus.media.images import normalize_image
from nexus.node.process import ExecutionStopped

AVD = 'NEXUS_Phone'
INPUT_PACKAGE = 'ai.nexus.emulatorinput'
INPUT_SERVICE = INPUT_PACKAGE + '/.PhoneInput'
PROTECTED = {'com.android.settings', 'com.android.permissioncontroller',
             'com.google.android.permissioncontroller', 'com.android.systemui',
             'android.system_overlay', 'ai.nexus.android'}


def properties(path):
    return {key.strip(): value.strip() for line in path.read_text(encoding='utf-8').splitlines()
            if '=' in line and not line.lstrip().startswith('#') for key, value in [line.split('=', 1)]}


def check_running(deadline, cancel):
    if cancel.is_set():
        raise ExecutionStopped('cancelled')
    if time.monotonic() >= deadline:
        raise ExecutionStopped('timeout')


class AndroidEmulator:
    @property
    def managed(self):
        return self.phone is not None

    def __init__(self, config):
        self.policy = config.plugins.android_emulator
        if self.policy is None or os.name != 'nt':
            raise ValueError('虚拟手机尚未配置 Windows Android 运行环境')
        self.root = config.data_dir / 'android-phone'
        self.avd = self.root / 'avd' / (AVD + '.avd')
        self.emulator = self.policy.sdk_dir / 'emulator/emulator.exe'
        self.adb = self.policy.sdk_dir / 'platform-tools/adb.exe'
        self.serial = 'emulator-' + str(self.policy.port)
        self.phone = None
        self.server = None
        self.lock = threading.RLock()
        self.frames = {}
        self.input_ready = False
        self.stopping = False

    def environment(self):
        return {'ANDROID_AVD_HOME': str(self.root / 'avd'),
                'ANDROID_USER_HOME': str(self.root / 'home'),
                'ANDROID_EMULATOR_HOME': str(self.root / 'home'),
                'ANDROID_SDK_ROOT': str(self.policy.sdk_dir),
                'ANDROID_ADB_SERVER_PORT': str(self.policy.adb_port),
                'ADB_SERVER_SOCKET': 'tcp:127.0.0.1:' + str(self.policy.adb_port),
                'ADB_VENDOR_KEYS': str(self.root / 'home/adbkey'),
                'ADB_MDNS_AUTO_CONNECT': '0'}

    def validate_runtime(self):
        if not self.emulator.is_file() or not self.adb.is_file():
            raise ValueError('找不到已配置的 Android 模拟器或 ADB')
        version = properties(self.emulator.parent / 'source.properties')
        image = properties(self.policy.system_image / 'source.properties')
        if version.get('Pkg.Revision') != self.policy.emulator_version or image.get('Pkg.Revision') != self.policy.image_revision:
            raise ValueError('Android 运行环境版本已变化，请先核对配置')
        if image.get('SystemImage.Abi') != 'x86_64' or not (self.policy.system_image / 'system.img').is_file():
            raise ValueError('虚拟手机需要已安装的 x86_64 系统镜像')
        return image

    def prepare(self):
        image = self.validate_runtime()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'home').mkdir(exist_ok=True)
        self.avd.mkdir(parents=True, exist_ok=True)
        marker = self.root / 'runtime.json'
        expected = {'avd': AVD, 'system_image': str(self.policy.system_image),
                    'emulator_version': self.policy.emulator_version, 'image_revision': self.policy.image_revision}
        if marker.exists():
            if json.loads(marker.read_text(encoding='utf-8')) != expected:
                raise ValueError('专用手机已使用另一运行环境；保留数据，请先处理版本迁移')
        else:
            # Refuse adoption of an unmarked pre-existing userdata image.
            if any(self.avd.iterdir()):
                raise ValueError('虚拟手机目录已有未登记数据，不能覆盖')
            marker.write_text(json.dumps(expected, ensure_ascii=False), encoding='utf-8')
        config = self.avd / 'config.ini'
        if not config.exists():
            values = {'AvdId': AVD, 'avd.ini.displayname': 'NEXUS Phone', 'abi.type': 'x86_64',
                      'hw.cpu.arch': 'x86_64', 'hw.cpu.ncore': '4', 'hw.ramSize': str(self.policy.memory_mb),
                      'hw.lcd.width': '720', 'hw.lcd.height': '1280', 'hw.lcd.density': '320',
                      'hw.gpu.enabled': 'yes', 'hw.gpu.mode': 'auto', 'hw.keyboard': 'yes',
                      'hw.audioInput': 'no', 'hw.audioOutput': 'no',
                      'hw.camera.back': 'none', 'hw.camera.front': 'none',
                      'disk.dataPartition.size': '8G', 'image.sysdir.1': str(self.policy.system_image) + os.sep,
                      'PlayStore.enabled': 'true' if 'playstore' in str(self.policy.system_image) else 'false',
                      'tag.id': self.policy.system_image.parent.name}
            config.write_text(''.join(f'{key}={value}\n' for key, value in values.items()), encoding='utf-8')
        registration = self.root / 'avd' / (AVD + '.ini')
        registration.write_text('avd.ini.encoding=UTF-8\npath=' + str(self.avd.resolve()) +
                                '\ntarget=android-' + image['AndroidVersion.ApiLevel'] + '\n', encoding='utf-8')

    @staticmethod
    def free_port(port):
        with socket.socket() as connection:
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                connection.bind(('127.0.0.1', port))
            except OSError:
                raise ValueError('虚拟手机专用端口已被占用；未接管其他程序') from None

    def start(self, deadline, cancel):
        from nexus.plugins.node_runtime import HostProcess
        if self.phone is not None and self.phone.process.poll() is None:
            if self.stopping:
                raise ValueError('虚拟手机正在关闭，请等状态变为已停止后再启动')
            return self.status(deadline, cancel)
        self._close_processes()
        self.prepare()
        for port in (self.policy.port, self.policy.port + 1, self.policy.adb_port):
            self.free_port(port)
        check_running(deadline, cancel)
        try:
            # ADB's documented localhost form binds network_loopback_server;
            # a numeric hostname is not accepted by this SDK. Never use -a.
            self.server = HostProcess([str(self.adb), '-L', 'tcp:localhost:' + str(self.policy.adb_port),
                                       '--one-device', 'nexus-virtual-phone-no-usb', 'server', 'nodaemon'], self.root, 'Local\\NEXUS-Phone-ADB-' + uuid4().hex,
                                      environment=self.environment())
            # Start our ADB server before the emulator tries to announce itself.
            while True:
                check_running(deadline, cancel)
                if self.server.process.poll() is not None:
                    raise ValueError('虚拟手机专用 ADB 服务启动失败: ' + self.server.output(1000,consume=False)['output'])
                try:
                    with socket.create_connection(('127.0.0.1', self.policy.adb_port), timeout=.1):
                        break
                except OSError:
                    time.sleep(.05)
            self.phone = HostProcess([str(self.emulator), '-avd', AVD, '-port', str(self.policy.port),
                                      '-no-snapshot', '-no-audio', '-no-boot-anim', '-gpu', 'auto',
                                      '-cores', '4', '-memory', str(self.policy.memory_mb),
                                      '-camera-back', 'none', '-camera-front', 'none'],
                                     self.root, 'Local\\NEXUS-Phone-' + uuid4().hex, environment=self.environment())
            check_running(deadline, cancel)
            return {'status': 'starting', 'name': 'NEXUS 虚拟手机', 'data_preserved': True,
                    'verification': 'owned_process_started'}
        except BaseException:
            self._close_processes()
            raise

    def command(self, arguments, deadline, cancel, *, max_bytes=65536):
        """ADB always targets our owned process and exact emulator serial."""
        if self.phone is None or self.phone.process.poll() is not None or self.server is None or self.server.process.poll() is not None:
            raise ValueError('虚拟手机未运行，请先启动')
        return self.run_command([str(self.adb), '-H', '127.0.0.1', '-P', str(self.policy.adb_port),
                                 '-s', self.serial, *arguments], deadline, cancel, max_bytes=max_bytes)

    def run_command(self, argv, deadline, cancel, *, max_bytes=65536):
        check_running(deadline, cancel)
        from nexus.plugins.node_runtime import HostProcess
        process = HostProcess(argv, self.root,
                              'Local\\NEXUS-Phone-Command-' + uuid4().hex, environment=self.environment(),buffer_limit=max_bytes+1)
        output = bytearray()
        try:
            while True:
                check_running(deadline, cancel)
                value = process.take_bytes(32768)
                if value['dropped'] or len(output) + len(value['bytes']) > max_bytes:
                    raise ValueError('虚拟手机响应超过容量')
                output.extend(value['bytes'])
                if value['exit_code'] is not None and not value['truncated']:
                    if value['exit_code']:
                        raise ValueError('虚拟手机命令未完成；请检查手机是否启动完成')
                    return bytes(output)
                time.sleep(.01)
        finally:
            process.stop()

    def verify_device(self, deadline, cancel):
        identity = self.command(['emu', 'avd', 'name'], deadline, cancel).decode('utf-8', 'replace').splitlines()
        if not identity or identity[0].strip() != AVD:
            raise ValueError('设备身份与专用虚拟手机不一致，已停止操作')

    def status(self, deadline, cancel):
        self.validate_runtime()
        if self.phone is None:
            return {'status': 'stopped', 'name': 'NEXUS 虚拟手机', 'data_preserved': True}
        if self.phone.process.poll() is not None:
            if self.stopping:
                self._close_processes()
                return {'status':'stopped','data_preserved':True,'verification':'owned_process_stopped'}
            return {'status': 'failed', 'name': 'NEXUS 虚拟手机', 'data_preserved': True,
                    'message': '模拟器已退出，请检查本机虚拟化和 Android 运行环境'}
        if self.stopping:
            return {'status':'stopping','data_preserved':True,'verification':'shutdown_requested'}
        try:
            self.verify_device(min(deadline, time.monotonic()+2), cancel)
            ready = self.command(['shell', 'getprop', 'sys.boot_completed'], deadline, cancel).strip() == b'1'
        except ValueError:
            ready = False
        except ExecutionStopped:
            check_running(deadline, cancel)
            ready = False
        return {'status': 'ready' if ready else 'starting', 'name': 'NEXUS 虚拟手机',
                'data_preserved': True, 'verification': 'adb_boot_property' if ready else 'owned_process_running'}

    def screenshot(self, deadline, cancel):
        self.verify_device(deadline, cancel)
        raw = self.command(['exec-out', 'screencap', '-p'], deadline, cancel, max_bytes=4*1024*1024)
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as picture:
            picture.load()
            pixels = picture.convert('RGB')
        state = self.command(['shell', 'dumpsys', 'window', 'displays'], deadline, cancel, max_bytes=262144).decode('utf-8', 'replace')
        matches = re.findall(r'mCurrentFocus=.*?\s([\w.]+)/[^\s}]+', state)
        # A resumed activity can be covered by a system error/permission dialog.
        # Still allow viewing that screen, but do not dispatch app input into it.
        return raw, pixels, matches[0] if matches else 'android.system_overlay'

    def observe(self, context, deadline, cancel):
        raw, pixels, package = self.screenshot(deadline, cancel)
        result = normalize_image(raw).model_dump(mode='json')
        digest = hashlib.sha256((context + uuid4().hex).encode() + raw).hexdigest()
        self.frames = {key: frame for key, frame in self.frames.items() if time.monotonic() - frame['time'] < 120}
        self.frames[context] = {'digest': digest, 'time': time.monotonic(), 'pixels': pixels,
                                'width': result['image']['width'], 'height': result['image']['height'], 'package': package}
        # One live observation per conversation, bounded across conversations.
        if len(self.frames) > 8:
            del self.frames[min(self.frames, key=lambda key: self.frames[key]['time'])]
        return {**result, 'observation_digest': digest, 'package': package, 'path': 'NEXUS virtual phone',
                'verification': 'emulator_screen_observed', 'notice': '按需截图；系统安全窗口可能为黑色。账号验证请在本机模拟器窗口完成。'}

    def act(self, name, value, context, deadline, cancel):
        frame = self.frames.get(context)
        if frame is None or frame['digest'] != value['observation_digest'] or time.monotonic() - frame['time'] >= 120:
            raise ValueError('画面已过期，请重新查看虚拟手机')
        _, current, package = self.screenshot(deadline, cancel)
        if package != frame['package'] or current.size != frame['pixels'].size:
            raise ValueError('前台应用或屏幕方向已改变，请重新查看')
        action = 'text' if name == 'type_text' else value['action']
        if package in PROTECTED and action not in {'home', 'back', 'recents'}:
            raise ValueError('请在模拟器窗口手动处理系统设置或授权')
        if action in {'tap', 'long_press', 'swipe'}:
            points = [(value.get('x'), value.get('y'))]
            if action == 'swipe':
                points.append((value.get('end_x'), value.get('end_y')))
            mapped = []
            for x, y in points:
                if type(x) is not int or type(y) is not int or not (0 <= x < frame['width'] and 0 <= y < frame['height']):
                    raise ValueError('坐标必须位于最近返回的图片内')
                px, py = x * current.width // frame['width'], y * current.height // frame['height']
                box = (max(0, px-24), max(0, py-24), min(current.width, px+25), min(current.height, py+25))
                if current.crop(box).tobytes() != frame['pixels'].crop(box).tobytes():
                    raise ValueError('目标区域已变化，请重新查看')
                mapped.extend((str(px), str(py)))
            arguments = ['shell', 'input', 'tap', *mapped] if action == 'tap' else ['shell', 'input', 'swipe', *(mapped if action == 'swipe' else mapped+mapped), '400' if action == 'swipe' else '700']
        elif action in {'home', 'back', 'recents', 'enter'}:
            arguments = ['shell', 'input', 'keyevent', {'home':'3', 'back':'4', 'recents':'187', 'enter':'66'}[action]]
        elif action == 'text':
            self.ensure_input(deadline, cancel)
            # Binding a newly selected IME is asynchronous. Probe readiness;
            # never use actual text submission as a retry/connection test.
            while True:
                check_running(deadline, cancel)
                ready = self.command(['shell', 'am', 'broadcast', '-a', INPUT_PACKAGE + '.READY',
                                      '-p', INPUT_PACKAGE, '--es', 'package', package], deadline, cancel)
                if b'result=1' in ready:
                    break
                time.sleep(.1)
            arguments = ['shell', 'am', 'broadcast', '-a', INPUT_PACKAGE + '.TEXT', '-p', INPUT_PACKAGE,
                         '--es', 'text', base64.b64encode(value['text'].encode('utf-8')).decode('ascii'),
                         '--es', 'package', package]
        else:
            raise ValueError('不支持的虚拟手机操作')
        # Consume before submitting any input; an unknown outcome is never replayed.
        del self.frames[context]
        output = self.command(arguments, deadline, cancel)
        if action == 'text' and b'result=1' not in output:
            raise ValueError('未能输入：请先聚焦普通输入框；密码和验证码请在模拟器窗口输入')
        return {'action': action, 'action_returned': True, 'verification': 'emulator_input_returned',
                'notice': '输入命令已返回；请重新查看画面核验实际结果'}

    def ensure_input(self, deadline, cancel):
        assets = Path(__file__).with_name('emulator_assets')
        manifest = json.loads((assets / 'input.json').read_text(encoding='utf-8'))
        apk = assets / 'nexus-input.apk'
        if hashlib.sha256(apk.read_bytes()).hexdigest() != manifest['sha256']:
            raise ValueError('随包中文输入助手校验失败')
        if not self.input_ready:
            installed = self.command(['shell', 'pm', 'list', 'packages', '-f', INPUT_PACKAGE], deadline, cancel).decode().strip()
            matches = False
            match = re.fullmatch(r'package:(/data/app/[A-Za-z0-9_./=+~-]+/base\.apk)=' + re.escape(INPUT_PACKAGE), installed)
            if match:
                digest = self.command(['shell', 'sha256sum', match[1]], deadline, cancel).decode().split()
                matches = bool(digest) and digest[0] == manifest['sha256']
            if not matches:
                result = self.command(['install', '-r', str(apk)], deadline, cancel)
                if b'Success' not in result:
                    raise ValueError('中文输入助手安装失败，原应用数据保持不变')
            self.input_ready = True
        selected = self.command(['shell', 'settings', 'get', 'secure', 'default_input_method'], deadline, cancel).decode().strip()
        if selected != INPUT_SERVICE:
            self.command(['shell', 'ime', 'enable', INPUT_SERVICE], deadline, cancel)
            self.command(['shell', 'ime', 'set', INPUT_SERVICE], deadline, cancel)

    def stop(self, deadline, cancel):
        if self.phone is None:
            return {'status': 'stopped', 'data_preserved': True}
        if self.phone.process.poll() is None:
            if self.stopping:
                return {'status':'stopping','data_preserved':True,'verification':'shutdown_requested'}
            self.verify_device(deadline, cancel)
            # emu kill terminates QEMU without a guest shutdown. Flush Android's
            # filesystem before closing so recently written app data survives.
            self.command(['shell', 'sync'], deadline, cancel)
            self.stopping = True
            self.command(['emu', 'kill'], deadline, cancel)
            return {'status':'stopping','data_preserved':True,'verification':'shutdown_requested'}
        self._close_processes()
        return {'status': 'stopped', 'data_preserved': True, 'verification': 'owned_process_stopped'}

    def _close_processes(self):
        for attribute in ('phone', 'server'):
            process = getattr(self, attribute)
            if process is not None:
                process.stop()
                setattr(self, attribute, None)
        self.frames.clear()
        self.input_ready = False
        self.stopping = False

    def close(self):
        with self.lock:
            try:
                self.stop(time.monotonic() + 5, threading.Event())
                until = time.monotonic()+15
                while self.phone is not None and self.phone.process.poll() is None and time.monotonic()<until:
                    time.sleep(.05)
                self._close_processes()
            except (ValueError, OSError, ExecutionStopped):
                self._close_processes()

    def execute(self, name, value, context, deadline, cancel):
        with self.lock:
            check_running(deadline, cancel)
            if name == 'status':
                return self.status(deadline, cancel)
            if name == 'start':
                return self.start(deadline, cancel)
            if name == 'stop':
                return self.stop(deadline, cancel)
            if self.stopping:
                raise ValueError('虚拟手机正在关闭，请等待完成')
            if name == 'observe':
                return self.observe(context, deadline, cancel)
            if name in {'act', 'type_text'}:
                return self.act(name, value, context, deadline, cancel)
            if name == 'list_apps':
                self.verify_device(deadline, cancel)
                installed = self.command(['shell', 'pm', 'list', 'packages', '-3'], deadline, cancel).decode().splitlines()
                packages = sorted(line[len('package:'):] for line in installed
                                  if re.fullmatch(r'package:[A-Za-z][A-Za-z0-9_.]+', line))
                return {'packages': packages, 'verification': 'installed_packages_observed'}
            if name == 'open_app':
                package = value['package']
                if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+', package):
                    raise ValueError('应用包名格式不正确')
                self.verify_device(deadline, cancel)
                resolved = self.command(['shell', 'cmd', 'package', 'resolve-activity', '--brief',
                                         '-a', 'android.intent.action.MAIN', '-c', 'android.intent.category.LAUNCHER', package], deadline, cancel).decode().strip().splitlines()
                component = resolved[-1] if resolved else ''
                if not re.fullmatch(re.escape(package) + r'/[A-Za-z0-9_.]+', component):
                    raise ValueError('该应用尚未安装或没有可打开的主界面')
                self.frames.clear()
                output = self.command(['shell', 'am', 'start', '-n', component], deadline, cancel)
                if b'Error' in output or b'Exception' in output:
                    raise ValueError('应用启动请求失败，请重新查看手机')
                return {'package': package, 'verification': 'app_launch_returned', 'notice': '请查看画面确认应用已打开'}
            raise ValueError('未知虚拟手机工具')
