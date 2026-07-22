"""
Tests for Phase 1: Real-time Gateway Session Sync.

Tests are ordered TDD-style:
  1. Gateway sessions appear in /api/sessions when setting enabled
  2. Gateway sessions excluded when setting disabled
  3. Gateway sessions have correct metadata (source_tag, is_cli_session)
  4. SSE stream endpoint opens and receives events
  5. Watcher detects new sessions inserted into state.db
  6. Settings UI has renamed label
"""
import json
import os
import pathlib
import sqlite3
import time
import urllib.error
import urllib.request

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
from tests._pytest_port import BASE


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read()), e.code
        except Exception:
            return {}, e.code


def _get_test_state_dir():
    """Return the test state directory (matches conftest.py TEST_STATE_DIR).

    conftest.py sets HERMES_WEBUI_TEST_STATE_DIR in the test-process environment
    (via os.environ.setdefault) so that tests writing directly to state.db always
    use the same path the test server was started with.  If the env var is not
    set (e.g. when running this file standalone), fall back to the conftest
    formula: HERMES_HOME/webui-mvp-test.
    """
    # Use _pytest_port which applies the same auto-derivation as conftest.py
    from tests._pytest_port import TEST_STATE_DIR as _ptsd
    return _ptsd


def _get_state_db_path():
    """Return path to the test state.db."""
    return _get_test_state_dir() / 'state.db'


def _ensure_state_db():
    """Create state.db with sessions and messages tables if it doesn't exist.
    Returns a connection. Does NOT delete existing data (safe for parallel tests).
    """
    db_path = _get_state_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            model TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            title TEXT
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            timestamp REAL NOT NULL
        );
    """)
    for column, ddl in (
        ('parent_session_id', 'ALTER TABLE sessions ADD COLUMN parent_session_id TEXT'),
        ('ended_at', 'ALTER TABLE sessions ADD COLUMN ended_at REAL'),
        ('end_reason', 'ALTER TABLE sessions ADD COLUMN end_reason TEXT'),
    ):
        existing = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if column not in existing:
            conn.execute(ddl)
    conn.commit()
    return conn


def _insert_gateway_session(conn, session_id='20260401_120000_abcdefgh', source='telegram',
                             title='Telegram Chat', model='anthropic/claude-sonnet-4-5',
                             started_at=None, message_count=2):
    """Insert a gateway session into state.db."""
    conn.execute(
        "INSERT OR REPLACE INTO sessions (id, source, title, model, started_at, message_count) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, source, title, model, started_at or time.time(), message_count)
    )
    # Delete any existing messages for this session (idempotent re-insert)
    conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
    # Insert some messages
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', ?, ?)",
        (session_id, 'Hello from Telegram', started_at or time.time())
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'assistant', ?, ?)",
        (session_id, 'Hi there!', (started_at or time.time()) + 1)
    )
    conn.commit()


def _insert_agent_session_row(
    conn,
    session_id,
    source='weixin',
    title='Agent Session',
    model='openai/gpt-5',
    started_at=None,
    parent_session_id=None,
    ended_at=None,
    end_reason=None,
    messages=1,
):
    """Insert an agent session row with optional compression lineage."""
    started_at = started_at or time.time()
    conn.execute(
        "INSERT OR REPLACE INTO sessions "
        "(id, source, title, model, started_at, message_count, parent_session_id, ended_at, end_reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            source,
            title,
            model,
            started_at,
            messages,
            parent_session_id,
            ended_at,
            end_reason,
        ),
    )
    conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
    for i in range(messages):
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (
                session_id,
                'user' if i % 2 == 0 else 'assistant',
                f'{title} message {i + 1}',
                started_at + i,
            ),
        )
    conn.commit()


def _remove_test_sessions(conn, *session_ids):
    """Remove specific test sessions from state.db (parallel-safe cleanup)."""
    for sid in session_ids:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
    conn.commit()


def _cleanup_state_db():
    """Remove state.db if it exists (only used for tests that need a blank slate)."""
    db_path = _get_state_db_path()
    for p in [db_path, db_path.parent / 'state.db-wal', db_path.parent / 'state.db-shm']:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


# ── Tests ──────────────────────────────────────────────────────────────────

def test_gateway_sessions_appear_when_enabled():
    """Gateway sessions from state.db appear in /api/sessions when show_cli_sessions is on."""
    conn = _ensure_state_db()
    try:
        _insert_gateway_session(conn, session_id='gw_test_tg_001', source='telegram', title='TG Test Chat')

        # Enable the setting
        post('/api/settings', {'show_cli_sessions': True})

        data, status = get('/api/sessions')
        assert status == 200
        sessions = data.get('sessions', [])
        gw_ids = [s['session_id'] for s in sessions if s.get('session_id') == 'gw_test_tg_001']
        assert len(gw_ids) == 1, f"Expected gateway session gw_test_tg_001, got {[s['session_id'] for s in sessions]}"
    finally:
        try:
            _remove_test_sessions(conn, 'gw_test_tg_001')
            conn.close()
        except Exception:
            pass
        post('/api/settings', {'show_cli_sessions': False})


def test_gateway_sessions_without_messages_are_hidden_from_sidebar():
    """Regression: empty agent session rows must not appear as broken sidebar entries."""
    conn = _ensure_state_db()
    empty_sid = 'gw_empty_no_messages_001'
    try:
        conn.execute(
            "INSERT OR REPLACE INTO sessions (id, source, title, model, started_at, message_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (empty_sid, 'cron', 'Cron Session', 'openai/gpt-5', time.time(), 0),
        )
        conn.execute("DELETE FROM messages WHERE session_id = ?", (empty_sid,))
        conn.commit()

        post('/api/settings', {'show_cli_sessions': True})

        data, status = get('/api/sessions')
        assert status == 200
        sessions = data.get('sessions', [])
        assert empty_sid not in {s.get('session_id') for s in sessions}, (
            "Agent sessions with no readable message rows should be filtered before "
            "they reach the sidebar; otherwise clicking them fails during import."
        )
    finally:
        try:
            _remove_test_sessions(conn, empty_sid)
            conn.close()
        except Exception:
            pass
        post('/api/settings', {'show_cli_sessions': False})


def test_gateway_watcher_hides_sessions_without_messages(monkeypatch):
    """Regression: SSE watcher must use the same importable-agent filter."""
    conn = _ensure_state_db()
    empty_sid = 'gw_empty_watcher_001'
    live_sid = 'gw_live_watcher_001'
    try:
        conn.execute(
            "INSERT OR REPLACE INTO sessions (id, source, title, model, started_at, message_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (empty_sid, 'telegram', 'Empty Telegram Session', 'openai/gpt-5', time.time(), 0),
        )
        conn.execute("DELETE FROM messages WHERE session_id = ?", (empty_sid,))
        _insert_gateway_session(
            conn,
            session_id=live_sid,
            source='telegram',
            title='Live Telegram Session',
            message_count=0,
        )

        import api.gateway_watcher as gateway_watcher

        monkeypatch.setattr(gateway_watcher, '_get_state_db_path', _get_state_db_path)

        sessions = gateway_watcher._get_agent_sessions_from_db()
        ids = {s.get('session_id') for s in sessions}
        live = next((s for s in sessions if s.get('session_id') == live_sid), None)

        assert empty_sid not in ids
        assert live is not None
        assert live.get('message_count') == 2, (
            "Watcher should fall back to actual message rows when stored "
            "message_count is zero, matching the sidebar route."
        )
    finally:
        try:
            _remove_test_sessions(conn, empty_sid, live_sid)
            conn.close()
        except Exception:
            pass


def test_compression_chain_collapses_to_latest_tip_in_sidebar():
    """Show one logical agent conversation for a compression continuation chain."""
    conn = _ensure_state_db()
    ids_to_remove = ('chain_root_001', 'chain_empty_mid_001', 'chain_tip_001')
    t0 = time.time() - 600
    try:
        _insert_agent_session_row(
            conn,
            'chain_root_001',
            title='Magazine Style PPT Skill',
            started_at=t0,
            ended_at=t0 + 100,
            end_reason='compression',
            messages=3,
        )
        _insert_agent_session_row(
            conn,
            'chain_empty_mid_001',
            title='Magazine Style PPT Skill #2',
            started_at=t0 + 101,
            parent_session_id='chain_root_001',
            ended_at=t0 + 200,
            end_reason='compression',
            messages=0,
        )
        _insert_agent_session_row(
            conn,
            'chain_tip_001',
            title='Magazine Style PPT Skill #3',
            started_at=t0 + 201,
            parent_session_id='chain_empty_mid_001',
            messages=2,
        )

        post('/api/settings', {'show_cli_sessions': True})
        data, status = get('/api/sessions')
        assert status == 200
        ids = {s.get('session_id') for s in data.get('sessions', [])}
        tip = next((s for s in data.get('sessions', []) if s.get('session_id') == 'chain_tip_001'), None)

        assert 'chain_tip_001' in ids
        assert 'chain_root_001' not in ids
        assert 'chain_empty_mid_001' not in ids
        assert tip is not None
        assert tip.get('title') == 'Magazine Style PPT Skill'
        assert tip.get('message_count') == 2
        # created_at = the chain head's started_at (preserves original conversation date)
        assert abs(tip.get('created_at') - t0) < 0.01
        # updated_at = the tip's last message timestamp so the sidebar entry
        # bubbles to the top by true recency, not by the root's stale activity.
        # tip messages are at t0+201 and t0+202, so last_activity = t0 + 202.
        assert abs(tip.get('updated_at') - (t0 + 202)) < 0.01

        from api.agent_sessions import read_importable_agent_session_rows

        rows = read_importable_agent_session_rows(_get_state_db_path(), limit=None)
        projected_tip = next((row for row in rows if row.get('id') == 'chain_tip_001'), None)
        assert projected_tip is not None
        assert projected_tip.get('title') == 'Magazine Style PPT Skill'
        assert projected_tip.get('_lineage_root_id') == 'chain_root_001'
        assert projected_tip.get('_lineage_tip_id') == 'chain_tip_001'
        assert projected_tip.get('_compression_segment_count') == 3
    finally:
        try:
            _remove_test_sessions(conn, *ids_to_remove)
            conn.close()
        except Exception:
            pass
        post('/api/settings', {'show_cli_sessions': False})


def test_compression_chain_with_empty_latest_tip_falls_back_to_latest_importable_segment():
    """Empty latest tips should not make the whole conversation disappear."""
    conn = _ensure_state_db()
    ids_to_remove = ('empty_tip_root_001', 'empty_tip_001')
    t0 = time.time() - 500
    try:
        _insert_agent_session_row(
            conn,
            'empty_tip_root_001',
            title='Long Conversation',
            started_at=t0,
            ended_at=t0 + 100,
            end_reason='compression',
            messages=2,
        )
        _insert_agent_session_row(
            conn,
            'empty_tip_001',
            title='Long Conversation #2',
            started_at=t0 + 101,
            parent_session_id='empty_tip_root_001',
            messages=0,
        )

        post('/api/settings', {'show_cli_sessions': True})
        data, status = get('/api/sessions')
        assert status == 200
        ids = {s.get('session_id') for s in data.get('sessions', [])}

        assert 'empty_tip_root_001' in ids
        assert 'empty_tip_001' not in ids
        root = next((s for s in data.get('sessions', []) if s.get('session_id') == 'empty_tip_root_001'), None)
        assert root and root.get('title') == 'Long Conversation'
    finally:
        try:
            _remove_test_sessions(conn, *ids_to_remove)
            conn.close()
        except Exception:
            pass
        post('/api/settings', {'show_cli_sessions': False})


def test_compression_chain_with_all_empty_segments_is_hidden():
    """A compression chain with no importable segment should not appear."""
    conn = _ensure_state_db()
    ids_to_remove = ('all_empty_root_001', 'all_empty_tip_001')
    t0 = time.time() - 450
    try:
        _insert_agent_session_row(
            conn,
            'all_empty_root_001',
            title='Empty Long Conversation',
            started_at=t0,
            ended_at=t0 + 100,
            end_reason='compression',
            messages=0,
        )
        _insert_agent_session_row(
            conn,
            'all_empty_tip_001',
            title='Empty Long Conversation #2',
            started_at=t0 + 101,
            parent_session_id='all_empty_root_001',
            messages=0,
        )

        post('/api/settings', {'show_cli_sessions': True})
        data, status = get('/api/sessions')
        assert status == 200
        ids = {s.get('session_id') for s in data.get('sessions', [])}

        assert 'all_empty_root_001' not in ids
        assert 'all_empty_tip_001' not in ids
    finally:
        try:
            _remove_test_sessions(conn, *ids_to_remove)
            conn.close()
        except Exception:
            pass
        post('/api/settings', {'show_cli_sessions': False})


def test_non_compression_child_is_not_collapsed_into_parent():
    """Parent/child relationships that are not compression continuations stay flat."""
    conn = _ensure_state_db()
    ids_to_remove = ('branch_parent_001', 'branch_child_001')
    t0 = time.time() - 400
    try:
        _insert_agent_session_row(
            conn,
            'branch_parent_001',
            title='Branch Parent',
            started_at=t0,
            ended_at=t0 + 100,
            end_reason='branched',
            messages=2,
        )
        _insert_agent_session_row(
            conn,
            'branch_child_001',
            title='Branch Child',
            started_at=t0 + 101,
            parent_session_id='branch_parent_001',
            messages=2,
        )

        from api.agent_sessions import read_importable_agent_session_rows

        rows = read_importable_agent_session_rows(_get_state_db_path(), limit=None)
        ids = {row.get('id') for row in rows}

        assert 'branch_parent_001' in ids
        assert 'branch_child_001' in ids
    finally:
        try:
            _remove_test_sessions(conn, *ids_to_remove)
            conn.close()
        except Exception:
            pass


def test_agent_session_limit_applies_after_compression_projection():
    """A long raw chain should count as one logical sidebar row before limiting."""
    conn = _ensure_state_db()
    chain_ids = [f'limit_chain_{i:03d}' for i in range(8)]
    standalone_id = 'limit_standalone_001'
    t0 = time.time() - 300
    try:
        for i, sid in enumerate(chain_ids):
            _insert_agent_session_row(
                conn,
                sid,
                title=f'Limit Chain #{i + 1}',
                started_at=t0 + i,
                parent_session_id=chain_ids[i - 1] if i else None,
                ended_at=t0 + i + 0.5 if i < len(chain_ids) - 1 else None,
                end_reason='compression' if i < len(chain_ids) - 1 else None,
                messages=1,
            )
        _insert_agent_session_row(
            conn,
            standalone_id,
            title='Limit Standalone',
            started_at=t0 + 20,
            messages=1,
        )

        from api.agent_sessions import read_importable_agent_session_rows

        rows = read_importable_agent_session_rows(_get_state_db_path(), limit=2)
        ids = [row.get('id') for row in rows]

        assert len(rows) == 2
        assert chain_ids[-1] in ids
        assert standalone_id in ids
        assert not any(sid in ids for sid in chain_ids[:-1])
        chain = next(row for row in rows if row.get('id') == chain_ids[-1])
        assert chain.get('title') == 'Limit Chain #1'
        assert chain.get('_lineage_root_id') == chain_ids[0]
        assert chain.get('_compression_segment_count') == len(chain_ids)
    finally:
        try:
            _remove_test_sessions(conn, *(chain_ids + [standalone_id]))
            conn.close()
        except Exception:
            pass


def test_compression_chain_bubbles_to_top_by_tip_activity():
    """An actively-used compression chain must surface in the sidebar by its
    TIP's last activity, not by the (stale) root's last activity.

    Without overriding ``last_activity`` from the tip, a long-running chain
    whose tip is being actively edited NOW would sort by the root's old
    timestamp and fall below recently touched standalone sessions — the
    inverse of what users expect from "Show agent sessions" sorted by
    recency. This regression test pins the override.
    """
    conn = _ensure_state_db()
    ids_to_remove = ('bubble_root_001', 'bubble_tip_001', 'bubble_standalone_001')
    now = time.time()
    # Root started long ago; tip is being edited "now" (very recent message)
    root_started = now - 30 * 86400
    root_ended = now - 28 * 86400
    tip_started = root_ended + 1
    tip_latest_msg = now - 5  # 5 seconds ago — most recent activity in the DB
    # A standalone session active 2 days ago — older than tip, much newer
    # than the root. Without the fix, the chain row sorts by ROOT's age and
    # standalone wins; with the fix, the chain wins.
    standalone_msg = now - 2 * 86400
    try:
        _insert_agent_session_row(
            conn,
            'bubble_root_001',
            title='Bubble Root',
            started_at=root_started,
            ended_at=root_ended,
            end_reason='compression',
            messages=2,
        )
        # Override message timestamps so root's last_activity is genuinely old.
        conn.execute("DELETE FROM messages WHERE session_id = 'bubble_root_001'")
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            ('bubble_root_001', 'user', 'old root msg', root_started + 60),
        )
        _insert_agent_session_row(
            conn,
            'bubble_tip_001',
            title='Bubble Tip',
            started_at=tip_started,
            parent_session_id='bubble_root_001',
            messages=1,
        )
        conn.execute("DELETE FROM messages WHERE session_id = 'bubble_tip_001'")
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            ('bubble_tip_001', 'user', 'fresh tip msg', tip_latest_msg),
        )
        _insert_agent_session_row(
            conn,
            'bubble_standalone_001',
            title='Bubble Standalone',
            started_at=now - 2 * 86400 - 60,
            messages=1,
        )
        conn.execute("DELETE FROM messages WHERE session_id = 'bubble_standalone_001'")
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            ('bubble_standalone_001', 'user', 'standalone msg', standalone_msg),
        )
        conn.commit()

        from api.agent_sessions import read_importable_agent_session_rows

        rows = read_importable_agent_session_rows(_get_state_db_path(), limit=200)
        ids = [row.get('id') for row in rows]
        # Filter out unrelated rows from the shared DB
        ids = [i for i in ids if i in ('bubble_root_001', 'bubble_tip_001', 'bubble_standalone_001')]

        assert 'bubble_tip_001' in ids, (
            f"Compression tip must appear in projected output. ids={ids}"
        )
        assert 'bubble_root_001' not in ids, (
            "Compression root row must be hidden once the tip is the active row."
        )

        tip_pos = ids.index('bubble_tip_001')
        standalone_pos = ids.index('bubble_standalone_001') if 'bubble_standalone_001' in ids else -1
        assert standalone_pos == -1 or tip_pos < standalone_pos, (
            f"Active compression tip (last msg 5s ago) must sort BEFORE standalone "
            f"session (last msg 2d ago). Got order: {ids}. "
            f"This indicates merged.last_activity is the root's stale value, "
            f"not the tip's recent value."
        )

        tip_row = next(r for r in rows if r['id'] == 'bubble_tip_001')
        assert abs(tip_row['last_activity'] - tip_latest_msg) < 0.01, (
            f"Projected tip's last_activity must equal the tip's most recent "
            f"message timestamp ({tip_latest_msg}), not the root's "
            f"({root_started + 60}). Got: {tip_row['last_activity']}"
        )
    finally:
        try:
            _remove_test_sessions(conn, *ids_to_remove)
            conn.close()
        except Exception:
            pass


def test_gateway_sessions_excluded_when_disabled():
    """Gateway sessions are NOT returned when show_cli_sessions is off."""
    conn = _ensure_state_db()
    try:
        _insert_gateway_session(conn, session_id='gw_test_dc_001', source='discord', title='DC Test Chat')

        # Ensure setting is off
        post('/api/settings', {'show_cli_sessions': False})

        data, status = get('/api/sessions')
        assert status == 200
        sessions = data.get('sessions', [])
        gw_ids = [s['session_id'] for s in sessions if s.get('session_id') == 'gw_test_dc_001']
        assert len(gw_ids) == 0, "Gateway session should not appear when setting is off"
    finally:
        try:
            _remove_test_sessions(conn, 'gw_test_dc_001')
            conn.close()
        except Exception:
            pass


def test_gateway_session_has_correct_metadata():
    """Gateway sessions include source_tag and is_cli_session fields."""
    conn = _ensure_state_db()
    try:
        _insert_gateway_session(conn, session_id='gw_meta_001', source='telegram', title='Meta Test')

        post('/api/settings', {'show_cli_sessions': True})

        data, status = get('/api/sessions')
        assert status == 200
        sessions = data.get('sessions', [])
        gw = next((s for s in sessions if s['session_id'] == 'gw_meta_001'), None)
        assert gw is not None, "Gateway session not found"
        assert gw.get('source_tag') == 'telegram', f"Expected source_tag=telegram, got {gw.get('source_tag')}"
        assert gw.get('is_cli_session') is True, "is_cli_session should be True for agent sessions"
        assert gw.get('title') == 'Meta Test'
    finally:
        try:
            _remove_test_sessions(conn, 'gw_meta_001')
            conn.close()
        except Exception:
            pass
        post('/api/settings', {'show_cli_sessions': False})


def test_imported_cron_sessions_hidden_from_sidebar_by_default(cleanup_test_sessions):
    """Cron sessions already imported into the WebUI store should stay hidden from the sidebar."""
    from api.models import Session

    sid = 'cron_imported_20260427'
    cleanup_test_sessions.append(sid)
    s = Session(
        session_id=sid,
        title='Hourly Cron Import',
        messages=[{'role': 'user', 'content': 'run hourly job', 'timestamp': time.time()}],
        model='openai/gpt-5',
    )
    s.is_cli_session = True
    s.save(touch_updated_at=False)

    data, status = get('/api/sessions')
    assert status == 200
    assert sid not in {session.get('session_id') for session in data.get('sessions', [])}


def test_cron_sessions_hidden_from_sidebar_by_default():
    """Cron-run sessions are background/internal and should not appear in the default sidebar list."""
    conn = _ensure_state_db()
    try:
        _insert_gateway_session(conn, session_id='cron_job123_20260427', source='cron', title='Nightly Cleanup')
        _insert_gateway_session(conn, session_id='gw_noncron_001', source='telegram', title='Visible Chat')

        post('/api/settings', {'show_cli_sessions': True})

        data, status = get('/api/sessions')
        assert status == 200
        sessions = data.get('sessions', [])
        ids = {s['session_id'] for s in sessions}
        assert 'gw_noncron_001' in ids, "Non-cron agent session should still appear"
        assert 'cron_job123_20260427' not in ids, "Cron session should be hidden by default"
    finally:
        try:
            _remove_test_sessions(conn, 'cron_job123_20260427', 'gw_noncron_001')
            conn.close()
        except Exception:
            pass
        post('/api/settings', {'show_cli_sessions': False})


def test_importable_agent_rows_can_opt_into_cron_source():
    """Diagnostic callers can opt out of the default cron exclusion explicitly."""
    conn = _ensure_state_db()
    try:
        _insert_agent_session_row(conn, session_id='cron_diag_20260427', source='cron', title='Cron Diagnostic')

        from api.agent_sessions import read_importable_agent_session_rows

        default_rows = read_importable_agent_session_rows(_get_state_db_path(), limit=None)
        assert 'cron_diag_20260427' not in {row.get('id') for row in default_rows}

        diagnostic_rows = read_importable_agent_session_rows(
            _get_state_db_path(),
            limit=None,
            exclude_sources=None,
        )
        assert 'cron_diag_20260427' in {row.get('id') for row in diagnostic_rows}
    finally:
        try:
            _remove_test_sessions(conn, 'cron_diag_20260427')
            conn.close()
        except Exception:
            pass


def test_gateway_session_has_message_count():
    """Gateway sessions report correct message_count from state.db."""
    conn = _ensure_state_db()
    try:
        _insert_gateway_session(conn, session_id='gw_msg_001', source='discord', title='Msg Count Test', message_count=5)

        post('/api/settings', {'show_cli_sessions': True})

        data, status = get('/api/sessions')
        assert status == 200
        sessions = data.get('sessions', [])
        gw = next((s for s in sessions if s['session_id'] == 'gw_msg_001'), None)
        assert gw is not None
        assert gw.get('message_count') == 5, f"Expected message_count=5, got {gw.get('message_count')}"
    finally:
        try:
            _remove_test_sessions(conn, 'gw_msg_001')
            conn.close()
        except Exception:
            pass
        post('/api/settings', {'show_cli_sessions': False})


def test_gateway_sessions_multiple_sources():
    """Sessions from multiple gateway sources (telegram, discord, slack) all appear."""
    conn = _ensure_state_db()
    try:
        _insert_gateway_session(conn, session_id='gw_multi_tg', source='telegram', title='TG Chat')
        _insert_gateway_session(conn, session_id='gw_multi_dc', source='discord', title='DC Chat')
        _insert_gateway_session(conn, session_id='gw_multi_sl', source='slack', title='SL Chat')

        post('/api/settings', {'show_cli_sessions': True})

        data, status = get('/api/sessions')
        assert status == 200
        sessions = data.get('sessions', [])
        gw_ids = {s['session_id'] for s in sessions if s.get('session_id') in ('gw_multi_tg', 'gw_multi_dc', 'gw_multi_sl')}
        assert len(gw_ids) == 3, f"Expected 3 gateway sessions, got {len(gw_ids)}: {gw_ids}"
    finally:
        try:
            _remove_test_sessions(conn, 'gw_multi_tg', 'gw_multi_dc', 'gw_multi_sl')
            conn.close()
        except Exception:
            pass
        post('/api/settings', {'show_cli_sessions': False})


def test_gateway_session_messages_readable():
    """Gateway session messages can be loaded via /api/session."""
    conn = _ensure_state_db()
    try:
        _insert_gateway_session(conn, session_id='gw_read_001', source='telegram', title='Readable')

        post('/api/settings', {'show_cli_sessions': True})

        data, status = get(f'/api/session?session_id=gw_read_001')
        assert status == 200
        msgs = data.get('session', {}).get('messages', [])
        assert len(msgs) >= 2, f"Expected at least 2 messages, got {len(msgs)}"
        assert msgs[0].get('role') == 'user'
        assert msgs[0].get('content') == 'Hello from Telegram'
    finally:
        try:
            _remove_test_sessions(conn, 'gw_read_001')
            conn.close()
        except Exception:
            pass
        post('/api/settings', {'show_cli_sessions': False})


def test_importing_older_gateway_session_preserves_original_timestamps_and_order():
    """Importing an older gateway session should not bump it above newer WebUI sessions."""
    conn = _ensure_state_db()
    older_started_at = time.time() - 1800
    imported_sid = 'gw_import_old_001'
    newer_webui_sid = None
    try:
        newer_webui, status = post('/api/session/new', {'model': 'openai/gpt-5'})
        assert status == 200, newer_webui
        newer_webui_sid = newer_webui['session']['session_id']

        rename, rename_status = post(
            '/api/session/rename',
            {'session_id': newer_webui_sid, 'title': 'Newer WebUI Session'},
        )
        assert rename_status == 200, rename

        _insert_gateway_session(
            conn,
            session_id=imported_sid,
            source='discord',
            title='Older imported gateway session',
            started_at=older_started_at,
        )
        post('/api/settings', {'show_cli_sessions': True})

        imported, imported_status = post('/api/session/import_cli', {'session_id': imported_sid})
        assert imported_status == 200, imported
        imported_session = imported['session']
        assert abs(imported_session['created_at'] - older_started_at) < 2, imported_session
        assert abs(imported_session['updated_at'] - older_started_at) < 5, imported_session

        sessions_payload, sessions_status = get('/api/sessions')
        assert sessions_status == 200, sessions_payload
        ordered_ids = [item['session_id'] for item in sessions_payload.get('sessions', [])]
        assert newer_webui_sid in ordered_ids, ordered_ids
        assert imported_sid in ordered_ids, ordered_ids
        assert ordered_ids.index(newer_webui_sid) < ordered_ids.index(imported_sid), ordered_ids
    finally:
        try:
            _remove_test_sessions(conn, imported_sid)
            conn.close()
        except Exception:
            pass
        if imported_sid:
            try:
                post('/api/session/delete', {'session_id': imported_sid})
            except Exception:
                pass
        if newer_webui_sid:
            try:
                post('/api/session/delete', {'session_id': newer_webui_sid})
            except Exception:
                pass
        post('/api/settings', {'show_cli_sessions': False})


def test_imported_gateway_session_stays_linked_to_newer_agent_state():
    """An imported sidecar must not shadow newer Telegram/CLI state.

    The WebUI copy is a cache. Once the authoritative Agent row advances, the
    sidebar should retain the CLI marker and surface newer metadata, and the
    idempotent import endpoint should extend only an exact-prefix transcript.
    """
    conn = _ensure_state_db()
    sid = 'gw_import_live_projection_001'
    started_at = time.time() - 300
    try:
        _insert_gateway_session(
            conn,
            session_id=sid,
            source='telegram',
            title='Live imported Telegram session',
            started_at=started_at,
            message_count=2,
        )
        post('/api/settings', {'show_cli_sessions': True})

        imported, imported_status = post('/api/session/import_cli', {'session_id': sid})
        assert imported_status == 200, imported
        assert imported['session']['is_cli_session'] is True
        assert len(imported['session']['messages']) == 2

        latest_at = time.time()
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (sid, 'user', 'A newer Telegram turn', latest_at - 1),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (sid, 'assistant', 'A newer Telegram response', latest_at),
        )
        conn.execute("UPDATE sessions SET message_count = 4 WHERE id = ?", (sid,))
        conn.commit()

        sessions_payload, sessions_status = get('/api/sessions')
        assert sessions_status == 200, sessions_payload
        matches = [s for s in sessions_payload.get('sessions', []) if s.get('session_id') == sid]
        assert len(matches) == 1, matches
        projected = matches[0]
        assert projected.get('is_cli_session') is True, projected
        assert projected.get('source_tag') == 'telegram', projected
        assert projected.get('message_count') == 2, projected
        assert projected.get('agent_remote_message_count') == 4, projected
        assert projected.get('updated_at', 0) >= latest_at, projected

        refreshed, refreshed_status = post('/api/session/import_cli', {'session_id': sid})
        assert refreshed_status == 200, refreshed
        assert refreshed['imported'] is False
        assert refreshed['session']['is_cli_session'] is True
        assert len(refreshed['session']['messages']) == 4

        loaded, loaded_status = get(f'/api/session?session_id={sid}')
        assert loaded_status == 200, loaded
        assert loaded['session'].get('is_cli_session') is True, loaded['session']
        assert len(loaded['session'].get('messages', [])) == 4
    finally:
        try:
            _remove_test_sessions(conn, sid)
            conn.close()
        except Exception:
            pass
        try:
            post('/api/session/delete', {'session_id': sid})
        except Exception:
            pass
        post('/api/settings', {'show_cli_sessions': False})


def test_imported_gateway_refresh_never_overwrites_diverged_webui_messages():
    """A hybrid sidecar is preserved when it differs from its sync watermark."""
    from api.models import Session, get_cli_session_messages, session_messages_hash

    conn = _ensure_state_db()
    sid = 'gw_import_diverged_projection_001'
    try:
        _insert_gateway_session(
            conn,
            session_id=sid,
            source='telegram',
            title='Diverged imported Telegram session',
            message_count=2,
        )
        original_messages = get_cli_session_messages(sid)
        assert len(original_messages) == 2
        local = Session(
            session_id=sid,
            title='Diverged imported Telegram session',
            messages=original_messages + [
                {'role': 'user', 'content': 'WebUI-only branch message'}
            ],
            profile='default',
            is_cli_session=True,
            source_tag='telegram',
            agent_session_id=sid,
            agent_lineage_root_id=sid,
            agent_sync_hash=session_messages_hash(original_messages),
            agent_sync_message_count=len(original_messages),
        )
        local.save(touch_updated_at=False)
        post('/api/settings', {'show_cli_sessions': True})

        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (sid, 'user', 'Different Telegram continuation', time.time()),
        )
        conn.execute("UPDATE sessions SET message_count = 3 WHERE id = ?", (sid,))
        conn.commit()

        refreshed, refreshed_status = post('/api/session/import_cli', {'session_id': sid})
        assert refreshed_status == 200, refreshed
        assert refreshed.get('conflict') is True, refreshed
        contents = [m.get('content') for m in refreshed['session']['messages']]
        assert contents[-1] == 'WebUI-only branch message', contents
        assert 'Different Telegram continuation' not in contents
        assert len(contents) == 3
    finally:
        try:
            _remove_test_sessions(conn, sid)
            conn.close()
        except Exception:
            pass
        try:
            post('/api/session/delete', {'session_id': sid})
        except Exception:
            pass
        post('/api/settings', {'show_cli_sessions': False})


def test_imported_cache_accepts_same_length_corrections_and_authoritative_shrink():
    """An unchanged cache may follow corrected or shortened Agent history."""
    conn = _ensure_state_db()
    sid = 'gw_import_rewrite_projection_001'
    try:
        _insert_gateway_session(conn, session_id=sid, source='telegram', message_count=2)
        post('/api/settings', {'show_cli_sessions': True})
        imported, status = post('/api/session/import_cli', {'session_id': sid})
        assert status == 200, imported

        conn.execute(
            "UPDATE messages SET content = ? WHERE session_id = ? AND role = 'assistant'",
            ('Corrected Agent response', sid),
        )
        conn.commit()
        corrected, status = post('/api/session/import_cli', {'session_id': sid})
        assert status == 200, corrected
        assert corrected.get('conflict') is False
        assert corrected['session']['messages'][-1]['content'] == 'Corrected Agent response'

        conn.execute("DELETE FROM messages WHERE session_id = ? AND role = 'assistant'", (sid,))
        conn.execute("UPDATE sessions SET message_count = 1 WHERE id = ?", (sid,))
        conn.commit()
        shrunk, status = post('/api/session/import_cli', {'session_id': sid})
        assert status == 200, shrunk
        assert shrunk.get('conflict') is False
        assert len(shrunk['session']['messages']) == 1
    finally:
        try:
            _remove_test_sessions(conn, sid)
            conn.close()
        except Exception:
            pass
        try:
            post('/api/session/delete', {'session_id': sid})
        except Exception:
            pass
        post('/api/settings', {'show_cli_sessions': False})


def test_compression_rotation_preserves_prior_visible_history():
    """Moving to a new Agent segment must append, never erase, earlier turns."""
    conn = _ensure_state_db()
    root_sid = 'gw_compression_preserve_root'
    tip_sid = 'gw_compression_preserve_tip'
    boundary = time.time() + 5
    try:
        _insert_gateway_session(
            conn,
            session_id=root_sid,
            source='telegram',
            title='Compression history',
            started_at=boundary - 100,
            message_count=2,
        )
        post('/api/settings', {'show_cli_sessions': True})
        imported, status = post('/api/session/import_cli', {'session_id': root_sid})
        assert status == 200, imported
        original = list(imported['session']['messages'])

        conn.execute(
            "UPDATE sessions SET ended_at = ?, end_reason = 'compression' WHERE id = ?",
            (boundary, root_sid),
        )
        _insert_agent_session_row(
            conn,
            tip_sid,
            source='telegram',
            title='Compression tip',
            started_at=boundary + 1,
            parent_session_id=root_sid,
            messages=2,
        )
        conn.commit()

        rotated, status = post('/api/session/import_cli', {'session_id': root_sid})
        assert status == 200, rotated
        assert rotated.get('conflict') is False
        assert rotated['session']['messages'][:len(original)] == original
        assert len(rotated['session']['messages']) == len(original) + 2
        assert rotated['session']['agent_session_id'] == tip_sid
        assert rotated['session']['agent_preserved_prefix_count'] == len(original)

        conn.execute(
            "UPDATE messages SET content = ? WHERE session_id = ? AND role = 'assistant'",
            ('Corrected compressed response', tip_sid),
        )
        conn.commit()
        corrected, status = post('/api/session/import_cli', {'session_id': root_sid})
        assert status == 200, corrected
        assert corrected['session']['messages'][:len(original)] == original
        assert len(corrected['session']['messages']) == len(original) + 2
        assert corrected['session']['messages'][-1]['content'] == 'Corrected compressed response'
    finally:
        try:
            _remove_test_sessions(conn, root_sid, tip_sid)
            conn.close()
        except Exception:
            pass
        try:
            post('/api/session/delete', {'session_id': root_sid})
        except Exception:
            pass
        post('/api/settings', {'show_cli_sessions': False})


def test_legacy_import_marker_survives_metadata_only_load(cleanup_test_sessions):
    """Old sidecars stored provenance after messages; deep links must recover it."""
    from api.models import Session

    sid = 'gw_legacy_metadata_projection_001'
    cleanup_test_sessions.append(sid)
    session = Session(
        session_id=sid,
        title='Legacy imported session',
        messages=[{'role': 'user', 'content': 'legacy'}],
        is_cli_session=True,
        source_tag='telegram',
    )
    session.save(touch_updated_at=False)
    path = session.path
    payload = json.loads(path.read_text(encoding='utf-8'))
    marker = payload.pop('is_cli_session')
    source = payload.pop('source_tag')
    messages = payload.pop('messages')
    tool_calls = payload.pop('tool_calls')
    legacy_payload = {**payload, 'messages': messages, 'tool_calls': tool_calls,
                      'is_cli_session': marker, 'source_tag': source}
    path.write_text(json.dumps(legacy_payload, ensure_ascii=False, indent=2), encoding='utf-8')

    metadata = Session.load_metadata_only(sid)
    assert metadata is not None
    assert metadata.is_cli_session is True
    assert metadata.source_tag == 'telegram'


def test_sibling_compression_continuations_remain_visible():
    from api.agent_sessions import _project_agent_session_rows

    root = {
        'id': 'root', 'started_at': 10, 'ended_at': 20,
        'end_reason': 'compression', 'actual_message_count': 2,
        'last_activity': 19, 'source': 'telegram',
    }
    older_branch = {
        'id': 'branch_a', 'parent_session_id': 'root', 'started_at': 21,
        'actual_message_count': 2, 'last_activity': 22, 'source': 'telegram',
    }
    canonical_tip = {
        'id': 'branch_b', 'parent_session_id': 'root', 'started_at': 23,
        'actual_message_count': 2, 'last_activity': 24, 'source': 'telegram',
    }
    projected = _project_agent_session_rows([root, older_branch, canonical_tip])
    assert {row['id'] for row in projected} == {'branch_a', 'branch_b'}
    canonical = next(row for row in projected if row['id'] == 'branch_b')
    assert canonical['_lineage_root_id'] == 'root'


def test_equal_timestamp_agent_messages_have_deterministic_id_order():
    from api.agent_sessions import read_agent_session_snapshot

    conn = _ensure_state_db()
    sid = 'gw_equal_timestamp_order_001'
    ts = time.time()
    try:
        _insert_agent_session_row(
            conn, sid, source='telegram', title='Equal timestamps',
            started_at=ts - 1, messages=0,
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (sid, 'user', 'first insertion', ts),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (sid, 'assistant', 'second insertion', ts),
        )
        conn.commit()
        snapshot = read_agent_session_snapshot(pathlib.Path(os.environ['HERMES_HOME']) / 'state.db', sid)
        assert [m['content'] for m in snapshot['messages']] == [
            'first insertion', 'second insertion'
        ]
    finally:
        _remove_test_sessions(conn, sid)
        conn.close()


def test_projection_is_profile_safe_and_follows_compression_lineage():
    from api.models import merge_imported_agent_session_projections

    local = {
        'session_id': 'root_sidecar', 'profile': 'default', 'is_cli_session': True,
        'agent_lineage_root_id': 'root_agent', 'message_count': 2,
        'updated_at': 100, 'last_message_at': 100,
    }
    tip = {
        'session_id': 'tip_agent', 'profile': 'default', 'is_cli_session': True,
        'agent_lineage_root_id': 'root_agent', 'message_count': 4,
        'updated_at': 300, 'last_message_at': 300, 'source_tag': 'telegram',
    }
    merged = merge_imported_agent_session_projections([local], [tip])
    assert len(merged) == 1
    assert merged[0]['session_id'] == 'root_sidecar'
    assert merged[0]['agent_session_id'] == 'tip_agent'
    assert merged[0]['message_count'] == 2
    assert merged[0]['agent_remote_message_count'] == 4
    assert merged[0]['last_message_at'] == 300

    wrong_profile = {**tip, 'session_id': 'root_sidecar', 'profile': 'work', 'message_count': 99}
    isolated = merge_imported_agent_session_projections([local], [wrong_profile])
    assert len(isolated) == 2
    assert isolated[0] == local
    work_row = isolated[1]
    assert work_row['profile'] == 'work'
    assert work_row['agent_session_id'] == 'root_sidecar'
    assert work_row['session_id'] != 'root_sidecar'
    assert work_row['session_id'].startswith('agent_')


def test_legacy_cache_accepts_exact_ordered_subsequence_but_not_local_branch():
    from api.models import session_messages_are_ordered_subsequence

    first = {'role': 'user', 'content': 'same', 'timestamp': 1}
    second = {'role': 'assistant', 'content': 'answer', 'timestamp': 2}
    authoritative = [first, dict(first), second]
    assert session_messages_are_ordered_subsequence([first, second], authoritative)
    assert not session_messages_are_ordered_subsequence(
        [first, {'role': 'user', 'content': 'WebUI-only', 'timestamp': 1.5}],
        authoritative,
    )


def test_missing_named_profile_fails_closed_without_touching_sidecar(cleanup_test_sessions):
    """A deleted profile must never fall back to a same-ID default transcript."""
    from api.models import Session, session_messages_hash

    sid = 'gw_missing_profile_fail_closed_001'
    cleanup_test_sessions.append(sid)
    local_messages = [{'role': 'user', 'content': 'Retired profile only copy'}]
    local = Session(
        session_id=sid,
        title='Retired profile cache',
        messages=local_messages,
        profile='retired-profile-that-does-not-exist',
        is_cli_session=True,
        source_tag='telegram',
        agent_session_id=sid,
        agent_lineage_root_id=sid,
        agent_sync_hash=session_messages_hash(local_messages),
        agent_sync_message_count=1,
    )
    local.save(touch_updated_at=False)

    conn = _ensure_state_db()
    try:
        _insert_gateway_session(
            conn,
            session_id=sid,
            source='telegram',
            title='Different default-profile transcript',
            message_count=2,
        )
        result, status = post('/api/session/import_cli', {'session_id': sid})
        assert status == 404, result
        reloaded = Session.load(sid)
        assert reloaded is not None
        assert reloaded.profile == 'retired-profile-that-does-not-exist'
        assert reloaded.messages == local_messages
        assert reloaded.agent_sync_conflict is False
    finally:
        _remove_test_sessions(conn, sid)
        conn.close()


def test_frontend_filters_imported_sessions_by_profile_and_tracks_rotated_tip():
    source = (REPO_ROOT / 'static' / 'sessions.js').read_text(encoding='utf-8')
    assert 'withMessages.filter(s=>s.profile===S.activeProfile)' in source
    assert 's.is_cli_session||s.profile===S.activeProfile' not in source
    assert 's.agent_lineage_root_id===activeRootId' in source
    assert "profile:s.profile||'default'" in source
    assert 'Agent sync conflict: local WebUI history was preserved' in source



def test_gateway_sse_stream_endpoint_exists():
    """GET /api/sessions/gateway/stream returns a response (200 or 200-range)."""
    # The SSE endpoint requires show_cli_sessions to be enabled
    post('/api/settings', {'show_cli_sessions': True})
    try:
        req = urllib.request.Request(BASE + '/api/sessions/gateway/stream')
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status in (200, 204), f"Expected 200/204, got {r.status}"
            # SSE should have content-type text/event-stream
            ctype = r.headers.get('Content-Type', '')
            assert 'text/event-stream' in ctype, f"Expected text/event-stream, got {ctype}"
    except Exception as e:
        # Timeout is acceptable — means the connection is held open (SSE behavior)
        if 'timed out' in str(e).lower() or 'timeout' in str(e).lower():
            pass  # Good: SSE keeps the connection open
        else:
            raise
    finally:
        post('/api/settings', {'show_cli_sessions': False})


def test_gateway_sse_stream_probe_reports_status():
    """Probe mode returns JSON watcher status instead of holding open an SSE stream."""
    post('/api/settings', {'show_cli_sessions': True})
    try:
        req = urllib.request.Request(BASE + '/api/sessions/gateway/stream?probe=1')
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200, f"Expected 200, got {r.status}"
            ctype = r.headers.get('Content-Type', '')
            assert 'application/json' in ctype, f"Expected application/json, got {ctype}"
            data = json.loads(r.read().decode('utf-8'))
            assert data['enabled'] is True
            assert 'watcher_running' in data
            assert data['fallback_poll_ms'] == 30000
    finally:
        post('/api/settings', {'show_cli_sessions': False})


def test_gateway_webui_sessions_not_duplicated():
    """If a session_id exists both in WebUI store and state.db, it's not duplicated."""
    # Create a WebUI session with a known ID
    body = {}
    d, _ = post('/api/session/new', body)
    webui_sid = d['session']['session_id']

    try:
        # Insert the same session_id into state.db as a gateway session
        conn = _ensure_state_db()
        _insert_gateway_session(conn, session_id=webui_sid, source='telegram', title='Dup Test')
        conn.close()

        post('/api/settings', {'show_cli_sessions': True})

        data, status = get('/api/sessions')
        assert status == 200
        sessions = data.get('sessions', [])
        matching = [s for s in sessions if s['session_id'] == webui_sid]
        assert len(matching) == 1, f"Expected 1 entry for {webui_sid}, got {len(matching)}"
    finally:
        try:
            conn2 = sqlite3.connect(str(_get_state_db_path()))
            _remove_test_sessions(conn2, webui_sid)
            conn2.close()
        except Exception:
            pass
        post('/api/session/delete', {'session_id': webui_sid})
        post('/api/settings', {'show_cli_sessions': False})


def test_import_cli_never_overwrites_native_same_id_collision(cleanup_test_sessions):
    """The bridge must not convert a native WebUI session that shares an Agent ID."""
    from api.models import Session

    sid = 'gw_native_collision_001'
    cleanup_test_sessions.append(sid)
    local_messages = [{'role': 'user', 'content': 'Native WebUI content must survive'}]
    native = Session(
        session_id=sid,
        title='Native WebUI session',
        messages=local_messages,
        is_cli_session=False,
    )
    native.save(touch_updated_at=False)

    conn = _ensure_state_db()
    try:
        _insert_gateway_session(
            conn,
            session_id=sid,
            source='telegram',
            title='Different Telegram session',
            message_count=2,
        )
        post('/api/settings', {'show_cli_sessions': True})

        result, status = post('/api/session/import_cli', {'session_id': sid})
        assert status == 200, result
        assert result.get('conflict') is True, result
        assert result['session']['is_cli_session'] is False
        assert result['session']['messages'] == local_messages

        reloaded = Session.load(sid)
        assert reloaded is not None
        assert reloaded.is_cli_session is False
        assert reloaded.messages == local_messages
    finally:
        try:
            _remove_test_sessions(conn, sid)
            conn.close()
        except Exception:
            pass
        post('/api/settings', {'show_cli_sessions': False})


def test_gateway_sessions_no_state_db():
    """When state.db doesn't exist, /api/sessions works fine (no gateway sessions)."""
    _cleanup_state_db()

    post('/api/settings', {'show_cli_sessions': True})
    try:
        data, status = get('/api/sessions')
        assert status == 200
        # Should succeed with just webui sessions (or empty)
        assert 'sessions' in data
    finally:
        post('/api/settings', {'show_cli_sessions': False})


def test_cli_sessions_still_work():
    """CLI sessions (source='cli') still appear alongside gateway sessions."""
    conn = _ensure_state_db()
    try:
        _insert_gateway_session(conn, session_id='cli_legacy_001', source='cli', title='CLI Legacy')
        _insert_gateway_session(conn, session_id='gw_new_001', source='telegram', title='GW New')

        post('/api/settings', {'show_cli_sessions': True})

        data, status = get('/api/sessions')
        assert status == 200
        sessions = data.get('sessions', [])
        agent_ids = {s['session_id'] for s in sessions if s.get('session_id') in ('cli_legacy_001', 'gw_new_001')}
        assert len(agent_ids) == 2, f"Expected 2 agent sessions (cli + gateway), got {len(agent_ids)}"
    finally:
        try:
            _remove_test_sessions(conn, 'cli_legacy_001', 'gw_new_001')
            conn.close()
        except Exception:
            pass
        post('/api/settings', {'show_cli_sessions': False})


# ── Unit tests for _gateway_sse_probe_payload ────────────────────────────────
# These replace the deleted repo-root test_gateway_sse_probe_unit.py and account
# for the watcher_alive check (thread existence + is_alive()).

import sys
import threading
sys.path.insert(0, str(REPO_ROOT))
from api.routes import _gateway_sse_probe_payload


def test_probe_payload_when_disabled():
    """Probe returns 404 when show_cli_sessions is False."""
    body, status = _gateway_sse_probe_payload({'show_cli_sessions': False}, watcher=None)
    assert status == 404
    assert body['ok'] is False
    assert body['enabled'] is False
    assert body['watcher_running'] is False
    assert body['error'] == 'agent sessions not enabled'
    assert body['fallback_poll_ms'] == 30000


def test_probe_payload_when_watcher_missing():
    """Probe returns 503 when enabled but no watcher instance."""
    body, status = _gateway_sse_probe_payload({'show_cli_sessions': True}, watcher=None)
    assert status == 503
    assert body['ok'] is False
    assert body['enabled'] is True
    assert body['watcher_running'] is False
    assert body['error'] == 'watcher not started'
    assert body['fallback_poll_ms'] == 30000


def test_probe_payload_when_watcher_instance_no_thread():
    """Probe returns 503 when watcher exists but _thread attribute is missing/None."""
    class _FakeWatcher:
        _thread = None
    body, status = _gateway_sse_probe_payload({'show_cli_sessions': True}, watcher=_FakeWatcher())
    assert status == 503
    assert body['watcher_running'] is False


def test_probe_payload_when_watcher_thread_alive():
    """Probe returns 200 when enabled and watcher thread is alive."""
    class _FakeWatcher:
        pass
    w = _FakeWatcher()
    t = threading.Thread(target=lambda: None)
    t.daemon = True
    t.start()
    w._thread = t
    # Thread may finish fast — loop-start a live daemon thread for reliability
    import time as _time
    done = threading.Event()
    live = threading.Thread(target=done.wait, daemon=True)
    live.start()
    w._thread = live
    try:
        body, status = _gateway_sse_probe_payload({'show_cli_sessions': True}, watcher=w)
        assert status == 200
        assert body['ok'] is True
        assert body['watcher_running'] is True
        assert body['fallback_poll_ms'] == 30000
    finally:
        done.set()
        live.join(timeout=1)


def test_probe_payload_when_watcher_thread_dead():
    """Probe returns 503 when watcher instance exists but thread has exited."""
    class _FakeWatcher:
        pass
    w = _FakeWatcher()
    t = threading.Thread(target=lambda: None)
    t.start()
    t.join()  # wait for it to finish
    w._thread = t
    body, status = _gateway_sse_probe_payload({'show_cli_sessions': True}, watcher=w)
    assert status == 503
    assert body['watcher_running'] is False
    assert body['ok'] is False


def test_gateway_watcher_is_alive_public_method():
    """GatewayWatcher.is_alive() is the public API the probe uses. Cover all
    three states: before start(), while running, after stop()."""
    from api.gateway_watcher import GatewayWatcher
    w = GatewayWatcher()
    # Before start(): no thread
    assert w.is_alive() is False, "is_alive() must be False before start()"
    # After start(): thread running
    w.start()
    try:
        assert w.is_alive() is True, "is_alive() must be True while running"
    finally:
        w.stop()
    # After stop(): thread cleared
    assert w.is_alive() is False, "is_alive() must be False after stop()"


def test_probe_payload_prefers_public_is_alive():
    """Regression guard: _gateway_sse_probe_payload must call watcher.is_alive()
    rather than poking at _thread directly when the public method exists."""
    calls = []

    class _WatcherWithPublicApi:
        def is_alive(self):
            calls.append('is_alive')
            return True
        # _thread is deliberately absent — must not be accessed.

    body, status = _gateway_sse_probe_payload(
        {'show_cli_sessions': True},
        watcher=_WatcherWithPublicApi(),
    )
    assert status == 200
    assert body['watcher_running'] is True
    assert calls == ['is_alive'], (
        "probe must prefer the public is_alive() method over poking _thread"
    )
