"""
tests/test_server_lifecycle.py — Test Suite for StudioServer Lifecycle & Shutdown
Validates StudioServerLifecycleMixIn, stop_serving(), and stop_studio_application().
"""

import http.server
import socket
import threading
import time
import unittest
import urllib.request

import studio.alerts as alerts
import studio.server as server


class FastHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{\"status\":\"ok\"}")

    def log_message(self, format, *args):
        pass


class BlockedHandler(http.server.BaseHTTPRequestHandler):
    started_event = threading.Event()
    unblock_event = threading.Event()

    def do_GET(self):
        BlockedHandler.started_event.set()
        BlockedHandler.unblock_event.wait(timeout=10.0)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


class MockClientSocket:
    def __init__(self):
        self.sent_data = bytearray()
        self.closed = False

    def sendall(self, data: bytes):
        if self.closed:
            raise OSError("Socket is closed")
        self.sent_data.extend(data)

    def close(self):
        self.closed = True


class StudioServerLifecycleTests(unittest.TestCase):
    def test_normal_startup_and_clean_shutdown(self):
        """Test real serve_forever() normal startup, serving request, and clean shutdown."""
        s = server.StudioServer(("127.0.0.1", 0), FastHandler)
        port = s.server_address[1]

        th = threading.Thread(target=s.serve_forever, daemon=True)
        th.start()
        self.assertTrue(s._loop_ready.wait(timeout=2.0), "Server failed to enter SERVING state")

        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=2.0)
        self.assertEqual(resp.status, 200)

        clean, diag = s.stop_serving(total_timeout=1.0)
        th.join(timeout=1.0)

        self.assertTrue(clean)
        self.assertEqual(s._lifecycle_state, server.ServerLifecycle.STOPPED)
        self.assertFalse(th.is_alive())
        self.assertTrue(diag["serving_loop_stopped"])
        self.assertTrue(diag["socket_closed"])
        self.assertEqual(diag["registered_handlers"], 0)
        self.assertEqual(diag["live_handler_threads"], 0)
        self.assertFalse(diag["timed_out"])

    def test_stop_before_startup_and_restart_rejection(self):
        """Test stop_serving before startup, attempted restart, and unit test admission rejection."""
        s = server.StudioServer(("127.0.0.1", 0), FastHandler)

        clean, diag = s.stop_serving(total_timeout=0.5)
        self.assertTrue(clean)
        self.assertEqual(s._lifecycle_state, server.ServerLifecycle.STOPPED)
        self.assertTrue(diag["socket_closed"])

        # Attempted restart must exit immediately
        t0 = time.monotonic()
        s.serve_forever()
        self.assertLess(time.monotonic() - t0, 0.05)
        self.assertEqual(s._lifecycle_state, server.ServerLifecycle.STOPPED)

        # Unit test: Direct admission rejection in process_request_thread
        mock_sock = MockClientSocket()
        s.process_request_thread(mock_sock, ("127.0.0.1", 12345))
        self.assertTrue(mock_sock.closed)
        self.assertIn(b"503 Service Unavailable", mock_sock.sent_data)

    def test_stopped_server_network_connection_refusal(self):
        """Integration test: Real TCP connection attempt is actively refused by OS kernel."""
        s = server.StudioServer(("127.0.0.1", 0), FastHandler)
        port = s.server_address[1]

        th = threading.Thread(target=s.serve_forever, daemon=True)
        th.start()
        self.assertTrue(s._loop_ready.wait(timeout=2.0))
        clean, diag = s.stop_serving(total_timeout=1.0)
        th.join(timeout=1.0)
        self.assertTrue(clean)

        real_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        connection_refused = False
        try:
            real_sock.connect(("127.0.0.1", port))
            self.fail("Network connection succeeded on closed port!")
        except ConnectionRefusedError as cre:
            connection_refused = True
            self.assertTrue(cre.errno in (10061, 111) or getattr(cre, "winerror", None) == 10061)
        except TimeoutError as te:
            self.fail(f"Connection timed out instead of being actively refused: {te}")
        except OSError as oe:
            if "timed out" in str(oe).lower():
                self.fail(f"Socket timed out instead of being refused: {oe}")
            win_err = getattr(oe, "winerror", None)
            if oe.errno in (10061, 111) or win_err == 10061:
                connection_refused = True
            else:
                raise
        finally:
            real_sock.close()

        self.assertTrue(connection_refused, "OS kernel must actively refuse connections when listening socket is closed")

    def test_blocked_handler_bounded_timeout(self):
        """Verify bounded shutdown and degraded reporting when a handler is blocked."""
        BlockedHandler.started_event.clear()
        BlockedHandler.unblock_event.clear()

        s = server.StudioServer(("127.0.0.1", 0), BlockedHandler)
        port = s.server_address[1]

        th = threading.Thread(target=s.serve_forever, daemon=True)
        th.start()
        self.assertTrue(s._loop_ready.wait(timeout=2.0))

        def trigger():
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/blocked", timeout=2.0)
            except Exception:
                pass

        req_th = threading.Thread(target=trigger, daemon=True)
        req_th.start()
        self.assertTrue(BlockedHandler.started_event.wait(timeout=3.0))

        t0 = time.monotonic()
        clean, diag = s.stop_serving(total_timeout=0.4)
        elapsed = time.monotonic() - t0

        th.join(timeout=1.0)
        self.assertFalse(clean)
        self.assertLess(elapsed, 0.7)
        self.assertTrue(diag["serving_loop_stopped"])
        self.assertTrue(diag["socket_closed"])
        self.assertGreaterEqual(diag["live_handler_threads"], 1)

        BlockedHandler.unblock_event.set()
        req_th.join(timeout=1.0)

    def test_stuck_serving_loop_leaves_socket_open(self):
        """Verify socket is NOT closed if the serving thread is still alive at deadline."""
        s = server.StudioServer(("127.0.0.1", 0), FastHandler)
        hold_loop = threading.Event()

        def stuck_loop():
            with s._lifecycle_lock:
                s._lifecycle_state = server.ServerLifecycle.SERVING
                s._serving_thread = threading.current_thread()
            s._loop_ready.set()
            hold_loop.wait(timeout=2.0)
            with s._lifecycle_lock:
                s._lifecycle_state = server.ServerLifecycle.STOPPED
            s._loop_terminated.set()

        stuck_th = threading.Thread(target=stuck_loop, daemon=True)
        stuck_th.start()
        self.assertTrue(s._loop_ready.wait(timeout=1.0))

        clean, diag = s.stop_serving(total_timeout=0.2)
        self.assertFalse(clean)
        self.assertFalse(diag["serving_loop_stopped"])
        self.assertFalse(diag["socket_closed"], "Socket must NOT be closed while serving loop is alive")
        self.assertTrue(stuck_th.is_alive())

        hold_loop.set()
        stuck_th.join(timeout=1.0)
        s.server_close()

    def test_app_shutdown_required_worker_alive(self):
        """Verify app shutdown reports unclean and degrades alert drain when a required worker remains alive."""
        s = server.StudioServer(("127.0.0.1", 0), FastHandler)
        th = threading.Thread(target=s.serve_forever, daemon=True)
        th.start()
        self.assertTrue(s._loop_ready.wait(timeout=2.0))

        hold_worker = threading.Event()
        def stuck_worker():
            hold_worker.wait(timeout=2.0)

        w_th = threading.Thread(target=stuck_worker, daemon=True)
        w_th.start()

        def dummy_alert_stop(timeout: float):
            return True, {"engine_state": "stopped", "timed_out": False}

        app_clean, app_diag = server.stop_studio_application(
            server=s,
            alert_engine_stop_fn=dummy_alert_stop,
            stop_event=threading.Event(),
            background_workers=[w_th],
            total_timeout=0.3,
        )
        th.join(timeout=1.0)

        self.assertFalse(app_clean)
        self.assertTrue(app_diag["http_clean"])
        self.assertFalse(app_diag["workers_clean"])
        self.assertEqual(app_diag["live_workers_count"], 1)
        # If a producer remains alive, report degraded shutdown and do not claim a clean drain
        self.assertFalse(app_diag["alerts_clean"], "Alert engine drain must not be claimed clean if producer is alive")
        self.assertFalse(app_diag["alerts_diag"]["producer_drain_clean"])

        hold_worker.set()
        w_th.join(timeout=1.0)

    def test_app_shutdown_ordering_workers_joined_before_alert_drain(self):
        """Verify that event-producing workers are stopped and joined BEFORE alert engine drains."""
        s = server.StudioServer(("127.0.0.1", 0), FastHandler)
        th = threading.Thread(target=s.serve_forever, daemon=True)
        th.start()
        self.assertTrue(s._loop_ready.wait(timeout=2.0))

        call_order = []
        stop_evt = threading.Event()

        def worker_loop():
            stop_evt.wait(timeout=1.0)
            call_order.append("worker_finished")

        w_th = threading.Thread(target=worker_loop, daemon=True)
        w_th.start()

        def mock_alert_stop(timeout: float):
            call_order.append("alert_stop_called")
            return True, {"engine_state": "stopped", "timed_out": False}

        app_clean, app_diag = server.stop_studio_application(
            server=s,
            alert_engine_stop_fn=mock_alert_stop,
            stop_event=stop_evt,
            background_workers=[w_th],
            total_timeout=1.0,
        )
        th.join(timeout=1.0)

        self.assertTrue(app_clean)
        self.assertEqual(call_order, ["worker_finished", "alert_stop_called"],
                         "Workers must finish and join BEFORE alert engine is stopped")

    def test_app_shutdown_alert_engine_timeout_propagation(self):
        """Verify timeout in alert engine propagates to app_clean=False and timed_out=True."""
        s = server.StudioServer(("127.0.0.1", 0), FastHandler)
        th = threading.Thread(target=s.serve_forever, daemon=True)
        th.start()
        self.assertTrue(s._loop_ready.wait(timeout=2.0))

        def timed_out_alert_stop(timeout: float):
            return False, {"timed_out": True, "drain_thread_healthy": False}

        app_clean, app_diag = server.stop_studio_application(
            server=s,
            alert_engine_stop_fn=timed_out_alert_stop,
            stop_event=threading.Event(),
            background_workers=[],
            total_timeout=0.3,
        )
        th.join(timeout=1.0)

        self.assertFalse(app_clean)
        self.assertTrue(app_diag["http_clean"])
        self.assertFalse(app_diag["alerts_clean"])
        self.assertTrue(app_diag["timed_out"])


    def test_blocked_http_handler_alert_enqueue_during_shutdown_window(self):
        """Verify producer quiescence barrier, degraded reporting, and no false clean accounting when handler is active."""
        import tempfile
        import os
        import sqlite3

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = tmp.name
        tmp.close()

        alerts.reset_engine_for_testing()
        alerts.start_alert_engine(db_path)

        handler_entered = threading.Event()
        release_handler = threading.Event()
        alert_outcome = {"marker": None, "attempted": False}

        class AlertEmittingHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                handler_entered.set()
                release_handler.wait(timeout=2.0)
                alert_outcome["attempted"] = True
                alert_outcome["marker"] = alerts.fire_alert(
                    fingerprint="http_active_alert",
                    component="http_handler",
                    reason="runtime_failure",
                    message="Failure during shutdown"
                )
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"DONE")

            def log_message(self, format, *args):
                pass

        s = server.StudioServer(("127.0.0.1", 0), AlertEmittingHandler)
        port = s.server_address[1]

        th = threading.Thread(target=s.serve_forever, daemon=True)
        th.start()
        self.assertTrue(s._loop_ready.wait(timeout=2.0))

        def client_request():
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/alert_task", timeout=3.0)
            except Exception:
                pass

        c_th = threading.Thread(target=client_request, daemon=True)
        c_th.start()
        self.assertTrue(handler_entered.wait(timeout=2.0))

        # Initiate shutdown while HTTP handler is actively blocked
        app_clean, app_diag = server.stop_studio_application(
            server=s,
            alert_engine_stop_fn=alerts.stop_alert_engine,
            stop_event=threading.Event(),
            background_workers=[],
            total_timeout=0.3,
        )

        # 1. Quiescence barrier must detect live handler and report degraded
        self.assertFalse(app_clean, "App shutdown must be degraded when HTTP handler is alive")
        self.assertFalse(app_diag["http_clean"])
        self.assertFalse(app_diag["producers_quiescent"], "Producers must NOT be reported quiescent")
        self.assertFalse(app_diag["alerts_diag"].get("accounting_complete", False),
                         "Accounting must NOT be falsely reported complete while handler is active")

        # 2. Release handler to allow it to attempt alert emission
        release_handler.set()
        c_th.join(timeout=1.0)
        th.join(timeout=1.0)

        # 3. Assert event was either accounted for or explicitly rejected without silent loss
        if alert_outcome["marker"] is None:
            # Explicitly rejected by intake closure
            self.assertGreater(alerts._DROPPED_ALERTS_COUNT, 0, "Rejected alert must increment dropped counter")
        else:
            # Accepted and accounted
            self.assertGreater(alerts._ACCEPTED_EVENTS_COUNT, 0)

        alerts.reset_engine_for_testing()
        try:
            os.remove(db_path)
        except Exception:
            pass

    def test_producer_racing_with_barrier_closure(self):
        """Verify race-safety and exact accounting when producers race with barrier closure."""
        import tempfile
        import os

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = tmp.name
        tmp.close()

        alerts.reset_engine_for_testing()
        alerts.start_alert_engine(db_path)

        num_producers = 4
        attempts_per_producer = 50
        total_attempts = num_producers * attempts_per_producer
        accepted_results = []
        rejected_results = []
        barrier = threading.Barrier(num_producers + 1)

        def producer_run(prod_id: int):
            barrier.wait()
            for i in range(attempts_per_producer):
                m = alerts.fire_alert(
                    fingerprint=f"racing_fp_{prod_id}_{i}",
                    component="worker",
                    reason="test_race",
                    message=f"Race attempt {i}"
                )
                if m is not None:
                    accepted_results.append(m)
                else:
                    rejected_results.append(f"{prod_id}_{i}")

        threads = [threading.Thread(target=producer_run, args=(p,)) for p in range(num_producers)]
        for t in threads:
            t.start()

        # Barrier release: producers start firing immediately
        barrier.wait()
        # Brief yield to let some through, then close barrier
        time.sleep(0.01)
        alerts.close_alert_intake()

        for t in threads:
            t.join(timeout=2.0)

        # Stop alert engine cleanly
        clean, diag = alerts.stop_alert_engine(timeout=3.0, producers_quiescent=True)

        self.assertEqual(len(accepted_results) + len(rejected_results), total_attempts)
        self.assertEqual(diag["accepted"], len(accepted_results))
        self.assertEqual(diag["dropped_alerts"], len(rejected_results))
        self.assertTrue(diag["accounting_matches"])
        self.assertTrue(diag["accounting_complete"])
        self.assertEqual(
            diag["committed"] + diag["dropped_stale"] + diag["persistence_failures"],
            diag["accepted"],
            "Every accepted event must receive exactly one terminal accounting outcome"
        )
        self.assertTrue(clean)

        alerts.reset_engine_for_testing()
        try:
            os.remove(db_path)
        except Exception:
            pass

    def test_enqueue_after_alert_intake_closes(self):
        """Verify that enqueue operations after alert intake closure are deterministically rejected."""
        import tempfile
        import os

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = tmp.name
        tmp.close()

        alerts.reset_engine_for_testing()
        alerts.start_alert_engine(db_path)

        alerts.close_alert_intake()

        # Attempt enqueue directly
        evt = alerts.AlertEvent("failure", "closed_fp", "test", "test", 100)
        accepted = alerts.enqueue_alert_event(evt)
        self.assertFalse(accepted, "enqueue_alert_event must return False after intake closure")

        # Attempt fire_alert
        marker = alerts.fire_alert("closed_fp_2", "test", "test")
        self.assertIsNone(marker, "fire_alert must return None after intake closure")

        # Exactly 2 rejected events accounted
        self.assertEqual(alerts._DROPPED_ALERTS_COUNT, 2)
        self.assertEqual(alerts._ACCEPTED_EVENTS_COUNT, 0)
        self.assertEqual(alerts._ALERT_QUEUE.qsize(), 0)

        clean, diag = alerts.stop_alert_engine(timeout=1.0, producers_quiescent=True)
        self.assertTrue(clean)
        self.assertEqual(diag["accepted"], 0)
        self.assertEqual(diag["dropped_alerts"], 2)

        alerts.reset_engine_for_testing()
        try:
            os.remove(db_path)
        except Exception:
            pass

    def test_repeated_shutdown_calls_idempotency(self):
        """Verify repeated calls to stop_studio_application() succeed cleanly and idempotently."""
        import tempfile
        import os

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = tmp.name
        tmp.close()

        alerts.reset_engine_for_testing()
        alerts.start_alert_engine(db_path)

        s = server.StudioServer(("127.0.0.1", 0), FastHandler)
        th = threading.Thread(target=s.serve_forever, daemon=True)
        th.start()
        self.assertTrue(s._loop_ready.wait(timeout=2.0))

        stop_evt = threading.Event()

        # Call 1: normal clean shutdown
        clean1, diag1 = server.stop_studio_application(
            server=s,
            alert_engine_stop_fn=alerts.stop_alert_engine,
            stop_event=stop_evt,
            background_workers=[],
            total_timeout=1.0,
        )
        th.join(timeout=1.0)
        self.assertTrue(clean1)
        self.assertTrue(diag1["app_clean"])

        # Call 2: repeated shutdown on already stopped application
        clean2, diag2 = server.stop_studio_application(
            server=s,
            alert_engine_stop_fn=alerts.stop_alert_engine,
            stop_event=stop_evt,
            background_workers=[],
            total_timeout=1.0,
        )
        self.assertTrue(clean2)
        self.assertTrue(diag2["app_clean"])

        # Call 3: third repeated shutdown
        clean3, diag3 = server.stop_studio_application(
            server=s,
            alert_engine_stop_fn=alerts.stop_alert_engine,
            stop_event=stop_evt,
            background_workers=[],
            total_timeout=1.0,
        )
        self.assertTrue(clean3)
        self.assertTrue(diag3["app_clean"])

        alerts.reset_engine_for_testing()
        try:
            os.remove(db_path)
        except Exception:
            pass

    def test_active_operator_transition_blocks_quiescence_and_accounting(self):
        """Verify that an active operator transition blocks producer quiescence and accounting completeness."""
        import tempfile
        import os
        import sqlite3

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = tmp.name
        tmp.close()

        alerts.reset_engine_for_testing()
        alerts.start_alert_engine(db_path)

        conn = sqlite3.connect(db_path, check_same_thread=False)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO alerts (id, fingerprint, component, reason, status, severity, message, occurrence_count,
                                created_at, last_seen_at, created_marker, last_marker)
            VALUES ('a_block', 'fp_block', 'comp', 'r', 'active', 'warning', 'msg', 1,
                    '2026-10-20T00:00:00Z', '2026-10-20T00:00:00Z', 1, 1)
        """)
        conn.commit()

        transition_in_flight = threading.Event()
        hold_transition = threading.Event()
        transition_done = threading.Event()
        transition_result = {}

        class InterceptingConnection:
            def __init__(self, real_conn):
                self._real_conn = real_conn
                self._intercepted = False

            def cursor(self):
                real_cur = self._real_conn.cursor()
                wrapper = self

                class InterceptingCursor:
                    def execute(self, sql, *args, **kwargs):
                        if not wrapper._intercepted:
                            wrapper._intercepted = True
                            transition_in_flight.set()
                            hold_transition.wait(timeout=5.0)
                        return real_cur.execute(sql, *args, **kwargs)

                    def fetchone(self):
                        return real_cur.fetchone()

                    def fetchall(self):
                        return real_cur.fetchall()

                    def __getattr__(self, name):
                        return getattr(real_cur, name)

                return InterceptingCursor()

            def __enter__(self):
                return self._real_conn.__enter__()

            def __exit__(self, *args):
                return self._real_conn.__exit__(*args)

            def __getattr__(self, name):
                return getattr(self._real_conn, name)

        conn_th = sqlite3.connect(db_path, check_same_thread=False)
        wrapped_conn = InterceptingConnection(conn_th)

        def run_trans():
            try:
                res = alerts.execute_operator_transition(wrapped_conn, 'resolve', 'a_block', 'operator', 'resolved')
                transition_result['res'] = res
            except Exception as e:
                transition_result['err'] = e
            finally:
                transition_done.set()

        th = threading.Thread(target=run_trans, daemon=True)
        th.start()
        self.assertTrue(transition_in_flight.wait(timeout=2.0))
        self.assertEqual(alerts.get_active_operator_transitions(), 1)

        # Initiate shutdown while transition is held in flight
        s = server.StudioServer(('127.0.0.1', 0), FastHandler)
        app_clean, app_diag = server.stop_studio_application(
            server=s,
            alert_engine_stop_fn=alerts.stop_alert_engine,
            stop_event=threading.Event(),
            background_workers=[],
            total_timeout=0.3,
        )

        self.assertFalse(app_clean, "App clean must be False when operator transition is active")
        self.assertFalse(app_diag["producers_quiescent"], "Producers quiescent must be False")
        self.assertEqual(app_diag["active_operator_transitions"], 1)
        self.assertFalse(app_diag["alerts_diag"]["no_operator_in_flight"], "no_operator_in_flight must be False")
        self.assertFalse(app_diag["alerts_diag"]["accounting_complete"], "accounting_complete must be False")

        # New transitions must be rejected once intake is closed
        with self.assertRaises(RuntimeError) as ctx:
            alerts.execute_operator_transition(conn, 'resolve', 'a_block', 'another_op')
        self.assertIn("Server is shutting down", str(ctx.exception))

        # Release the held transition
        hold_transition.set()
        self.assertTrue(transition_done.wait(timeout=2.0))
        th.join(timeout=1.0)
        self.assertEqual(alerts.get_active_operator_transitions(), 0)

        # Repeated shutdown must now be clean
        clean2, diag2 = server.stop_studio_application(
            server=s,
            alert_engine_stop_fn=alerts.stop_alert_engine,
            stop_event=threading.Event(),
            background_workers=[],
            total_timeout=1.0,
        )
        self.assertTrue(clean2, "Repeated shutdown must be clean after transition unregisters")
        self.assertTrue(diag2["producers_quiescent"], "Producers must be quiescent")
        self.assertEqual(diag2["active_operator_transitions"], 0)
        self.assertTrue(diag2["alerts_diag"]["no_operator_in_flight"])
        self.assertTrue(diag2["alerts_diag"]["accounting_complete"])

        conn.close()
        conn_th.close()
        alerts.reset_engine_for_testing()
        try:
            os.remove(db_path)
        except Exception:
            pass

if __name__ == "__main__":
    unittest.main()
