"""Open a qualified recovery target through the existing local launcher."""
from pathlib import Path
import os
import subprocess
import sys
import threading
import time
from urllib.parse import quote

from workbench.instance import existing_instance_url
from workbench.recovery_sessions import RecoverySessionError
from workbench.restore_scope import load_restore_scope


class RecoveryTargetLauncher:
    def __init__(self, *, repository, python_executable=None, machine_store=None,
                 process_factory=None, startup_timeout=10.0):
        self.repository = Path(repository).resolve(strict=True)
        self.python_executable = str(python_executable or sys.executable)
        self.machine_store = machine_store
        self.process_factory = process_factory or subprocess.Popen
        self.startup_timeout = startup_timeout
        self._lock = threading.Lock()
        self._pending = {}

    def _require_target(self, session):
        if (session['phase'] != 'active' or session['pending_action'] is not None
                or session['activated'] is not True):
            raise RecoverySessionError('请先完成恢复副本的启用，再打开作品。')
        target = Path(session['destination'])
        scope = load_restore_scope(target/'workbench.sqlite', machine_store=self.machine_store)
        if (scope is None or not scope.active or scope.project_id != session['project_id']
                or scope.document['book_id'] != session['book_id']
                or scope.document['plan_sha256'] != session['plan_sha256']):
            raise RecoverySessionError('恢复副本的当前写资格或目标身份无法核验，未打开作品。')
        return target

    def open(self, session):
        # The child acquires its own instance lease. Do not inherit the source
        # lease, and never infer successful startup from a PID or process exit.
        with self._lock:
            target = self._require_target(session)
            url = existing_instance_url(target, timeout=0)
            if url:
                return self._result(session, url)
            process = self._pending.get(str(target))
            if process is None or process.poll() is not None:
                command = [self.python_executable, '-B', str(self.repository/'run_workbench.py'),
                    '--restore-dir', str(target), '--no-browser', '--port', '0']
                options = dict(cwd=str(self.repository), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                    env=dict(os.environ, PYTHONUTF8='1'))
                if os.name == 'nt':
                    options['creationflags'] = subprocess.CREATE_NO_WINDOW
                try:
                    process = self.process_factory(command, **options)
                except OSError as exc:
                    raise RecoverySessionError('无法启动恢复工作台，请核对本项目的Python环境与启动器。') from exc
                self._pending[str(target)] = process
            deadline = time.monotonic() + self.startup_timeout
            while True:
                url = existing_instance_url(target, timeout=0)
                if url:
                    self._require_target(session)
                    return self._result(session, url)
                if process.poll() is not None:
                    raise RecoverySessionError('恢复工作台尚未确认启动。请核对副本资格、数据目录占用和启动环境后再次打开。')
                if time.monotonic() >= deadline:
                    # Observation timeout is not termination. A subsequent
                    # explicit click continues observing this original child.
                    raise RecoverySessionError('副本仍在启动，尚未确认访问地址。请稍后再次打开以核对原实例。')
                time.sleep(.1)

    @staticmethod
    def _result(session, url):
        return {'session_id':session['session_id'], 'project_id':session['project_id'],
            'url':url + '#/projects/' + quote(session['project_id'], safe='') + '/overview',
            'automatic_provider_calls_allowed':False}
