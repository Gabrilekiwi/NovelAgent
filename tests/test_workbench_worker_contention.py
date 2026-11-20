"""SQLite contention before claim must not kill the actual worker thread."""
import sqlite3,threading

import pytest

from tests.test_maintenance_integration import supervisor_case


def test_worker_survives_real_busy_writer_then_executes_original_job_once(tmp_path,monkeypatch):
    store,project,lease,supervisor=supervisor_case(tmp_path)
    supervisor._recover_startup_once()
    job=store.create_job(project_id=project,kind='generation_session',start_chapter=1,end_chapter=1)
    blocked=threading.Event();finished=threading.Event();executed=[];errors=[]
    original_open=store.database._open;original_claim=store.claim_next
    def short_wait():
        connection=original_open();connection.execute('PRAGMA busy_timeout = 25');return connection
    monkeypatch.setattr(store.database,'_open',short_wait)
    def observed():
        try:return original_claim()
        except sqlite3.OperationalError as exc:
            errors.append(exc.sqlite_errorcode);blocked.set();raise
    monkeypatch.setattr(store,'claim_next',observed)
    def execute(selected):
        executed.append(selected.id);supervisor._stop.set();finished.set()
    monkeypatch.setattr(supervisor,'_run_generation_session',execute)
    try:
        with store.database.transaction():
            supervisor._thread=threading.Thread(target=supervisor._worker_loop,name='actual-busy-worker')
            supervisor._thread.start()
            assert blocked.wait(5),'actual SQLite contention was not observed'
            assert supervisor._thread.is_alive() and not executed
        supervisor.notify();assert finished.wait(5)
        supervisor.shutdown()
        assert executed==[job.id] and errors and all(code & 0xff in {sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED} for code in errors)
        assert supervisor._maintenance.status()['active_writes']==0
    finally:supervisor.shutdown();lease.close()


def test_claim_database_syntax_error_is_not_treated_as_temporary_contention(tmp_path,monkeypatch):
    store,project,lease,supervisor=supervisor_case(tmp_path)
    def invalid():
        with store.database.connection() as connection:connection.execute('THIS IS NOT SQL')
    monkeypatch.setattr(store,'claim_next',invalid)
    try:
        with pytest.raises(sqlite3.OperationalError):supervisor._run_one_job()
    finally:supervisor.shutdown();lease.close()
