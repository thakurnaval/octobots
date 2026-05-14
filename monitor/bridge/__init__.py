"""Octobots supervisor monitor bridge.

Tails relay.db, tmux panes, and notify.log; mirrors per-task activity
into `.agents/transcripts/<role>/`; and serves a small HTTP+WS endpoint
at http://127.0.0.1:2469 that the monitor UI (and any other consumer)
reads from + posts inbound messages to.

Run: `python3 -m monitor.bridge` from the supervisor/ directory.
"""

__version__ = "0.2.0"
