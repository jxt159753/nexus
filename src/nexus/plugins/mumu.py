"""Control one owner-selected, existing MuMu instance through its local CLI."""

import json
import os
import socket
import threading
import time
from uuid import uuid4

from nexus.node.process import ExecutionStopped
from nexus.plugins.android_emulator import AndroidEmulator, check_running


class MuMu(AndroidEmulator):
    def __init__(self, config):
        if os.name != 'nt':
            raise ValueError('MuMu 虚拟手机需要 Windows')
        self.policy = config.plugins.android_emulator
        self.root = config.data_dir / 'mumu-phone'
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'home').mkdir(exist_ok=True)
        self.cli = self.policy.install_dir / 'nx_main/mumu-cli.exe'
        self.adb = self.cli.with_name('adb.exe')
        self.server = None
        self.serial = None
        self.connection = None
        self.lock = threading.RLock()
        self.frames = {}
        self.input_ready = False
        self.stopping = False
        self.owned = False
        self.launch_until = 0
        self.operation_info = None
        self.in_operation = False

    @property
    def managed(self):
        return self.owned

    def environment(self):
        return {'ANDROID_USER_HOME': str(self.root / 'home'),
                'ANDROID_ADB_SERVER_PORT': str(self.policy.adb_port),
                'ADB_SERVER_SOCKET': 'tcp:127.0.0.1:' + str(self.policy.adb_port),
                'ADB_VENDOR_KEYS': str(self.root / 'home/adbkey'), 'ADB_MDNS_AUTO_CONNECT': '0'}

    def control(self, args, deadline, cancel):
        raw = self.run_command([str(self.cli), *args], deadline, cancel)
        result = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(result, dict) or result.get('errcode', result.get('error_code', 0)) != 0:
            raise ValueError('MuMu 命令失败，请检查本机模拟器状态')
        return result

    def info(self, deadline, cancel):
        check_running(deadline, cancel)
        if getattr(self, 'in_operation', False) and self.operation_info is not None:
            return self.operation_info
        if not self.cli.is_file() or not self.adb.is_file():
            raise ValueError('找不到已配置的 MuMu 安装目录')
        version = self.control(['version'], deadline, cancel)
        if version.get('version') != self.policy.version:
            raise ValueError('MuMu 版本已变化，请先核对运行配置')
        result = self.control(['info', '-v', str(self.policy.instance)], deadline, cancel)
        if (str(result.get('index')) != str(self.policy.instance)
                or result.get('created_timestamp') != self.policy.created_timestamp):
            raise ValueError('MuMu 实例身份已变化，未接管其他实例')
        if getattr(self, 'in_operation', False):
            self.operation_info = result
        return result

    def execute(self, name, value, context, deadline, cancel):
        # One instance snapshot per bounded plugin operation. All commands in
        # that operation use its exact endpoint; the next call rechecks identity.
        with self.lock:
            self.in_operation = True
            self.operation_info = None
            try:
                return super().execute(name, value, context, deadline, cancel)
            finally:
                self.operation_info = None
                self.in_operation = False

    def start(self, deadline, cancel):
        if self.stopping:
            raise ValueError('MuMu 正在关闭，请等状态变为已停止后再启动')
        info = self.info(deadline, cancel)
        if info.get('is_process_started') or self.launch_until:
            return self.status(deadline, cancel)
        # Remember the launch before issuing it. Cancellation/unknown outcomes
        # must be reconciled with info, never retried as another launch.
        self.owned = True
        self.launch_until = time.monotonic() + 60
        self.control(['control', '-v', str(self.policy.instance), 'launch'], deadline, cancel)
        return {'status': 'starting', 'name': 'MuMu 虚拟手机', 'data_preserved': True,
                'verification': 'mumu_launch_requested'}

    def connect(self, info, deadline, cancel):
        from nexus.plugins.node_runtime import HostProcess
        host, port = info.get('adb_host_ip'), info.get('adb_port')
        if host != '127.0.0.1' or type(port) is not int or not 1024 <= port <= 65535 or port == self.policy.adb_port:
            raise ValueError('MuMu 未提供有效的本机 ADB 地址')
        serial = f'{host}:{port}'
        identity = (serial, info.get('pid'))
        if self.connection is not None and self.connection != identity:
            self.frames.clear()
            self.input_ready = False
            self.connection = None
            raise ValueError('MuMu 实例已重启，请重新查看画面')
        if self.server is None:
            self.free_port(self.policy.adb_port)
            self.server = HostProcess([str(self.adb), '-L', 'tcp:localhost:' + str(self.policy.adb_port),
                                       '--one-device', 'nexus-virtual-phone-no-usb', 'server', 'nodaemon'],
                                      self.root, 'Local\\NEXUS-MuMu-ADB-' + uuid4().hex, environment=self.environment())
            while True:
                check_running(deadline, cancel)
                if self.server.process.poll() is not None:
                    raise ValueError('MuMu 专用 ADB 服务启动失败')
                try:
                    with socket.create_connection(('127.0.0.1', self.policy.adb_port), timeout=.1):
                        break
                except OSError:
                    time.sleep(.05)
        if self.server.process.poll() is not None:
            raise ValueError('MuMu 专用 ADB 已退出，请停止后重新启动')
        self.serial = serial
        if self.connection != identity:
            result = self.run_command([str(self.adb), '-H', '127.0.0.1', '-P', str(self.policy.adb_port),
                                       'connect', serial], deadline, cancel)
            if ('connected to ' + serial).encode() not in result:
                raise ValueError('MuMu 尚未接受 ADB 连接，请稍后查询状态')
            self.connection = identity

    def command(self, arguments, deadline, cancel, *, max_bytes=65536):
        info = self.info(deadline, cancel)
        if not info.get('is_android_started'):
            raise ValueError('MuMu 尚未启动完成')
        self.connect(info, deadline, cancel)
        return self.run_command([str(self.adb), '-H', '127.0.0.1', '-P', str(self.policy.adb_port),
                                 '-s', self.serial, *arguments], deadline, cancel, max_bytes=max_bytes)

    def verify_device(self, deadline, cancel):
        # command verifies the configured immutable instance identity and only
        # connects to the loopback endpoint reported by that instance's CLI.
        self.command(['get-state'], deadline, cancel)

    def status(self, deadline, cancel):
        info = self.info(deadline, cancel)
        if not info.get('is_process_started'):
            if self.launch_until:
                if time.monotonic() < self.launch_until:
                    return {'status': 'starting', 'name': 'MuMu 虚拟手机', 'data_preserved': True}
                self.launch_until = 0
                return {'status': 'failed', 'message': 'MuMu 启动未完成，请检查模拟器窗口', 'data_preserved': True}
            self._close_processes()
            self.owned = False
            return {'status': 'stopped', 'name': 'MuMu 虚拟手机', 'data_preserved': True,
                    'verification': 'mumu_instance_stopped'}
        self.launch_until = 0
        if self.stopping:
            return {'status': 'stopping', 'data_preserved': True, 'verification': 'shutdown_requested'}
        ready = info.get('is_android_started') and self.command(['shell', 'getprop', 'sys.boot_completed'], deadline, cancel).strip() == b'1'
        return {'status': 'ready' if ready else 'starting', 'name': 'MuMu 虚拟手机', 'data_preserved': True,
                'verification': 'adb_boot_property' if ready else 'mumu_instance_running'}

    def stop(self, deadline, cancel):
        info = self.info(deadline, cancel)
        if not info.get('is_process_started') and not self.launch_until:
            return self.status(deadline, cancel)
        if not self.stopping:
            if info.get('is_android_started'):
                self.command(['shell', 'sync'], deadline, cancel)
            self.stopping = True
            self.launch_until = 0
            self.control(['control', '-v', str(self.policy.instance), 'shutdown'], deadline, cancel)
        return {'status': 'stopping', 'data_preserved': True, 'verification': 'shutdown_requested'}

    def _close_processes(self):
        if self.server is not None:
            self.server.stop()
            self.server = None
        self.connection = None
        self.frames.clear()
        self.input_ready = False
        self.stopping = False

    def close(self):
        with self.lock:
            try:
                if self.owned:
                    until = time.monotonic() + 25
                    self.stop(until, threading.Event())
                    while self.owned and time.monotonic() < until:
                        self.status(until, threading.Event())
                        time.sleep(.1)
            except (ValueError, OSError, ExecutionStopped):
                # MuMu is an external installed runtime: never kill its service
                # or VM process tree when graceful shutdown is uncertain.
                pass
            finally:
                self._close_processes()
