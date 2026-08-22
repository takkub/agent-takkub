"""Shared teardown helper for #344 (silent PyQt6 abort from a QTimer leaked
by an earlier test firing under the session-wide QApplication).

A widget/QObject whose __init__ starts a QTimer unconditionally (CliServer,
ProjectNav, ProjectTab, LeadNotifier, ...) keeps that timer armed for as long
as the instance is reachable — which, once a Qt signal is connected to one of
its bound methods, can outlive the test that created it (the connection holds
a strong reference back to `self`, deferring collection to whenever cyclic GC
happens to run). `stop_timers_after` patches the class's __init__ to record
every instance built while a test runs, then stops the named timers at
teardown — so a leftover instance can never still be armed once the process
moves on to a later test/module and pumps the event loop again.

Each name in *timer_attrs* is either a QTimer attribute (stopped directly) or
a zero-arg cleanup method like `CliServer.shutdown_timers` (#345 — called
instead, for a class whose timers aren't all one fixed attribute — e.g. one
spawn-stagger QTimer per staggered dispatch).
"""

from __future__ import annotations

from collections.abc import Callable


def stop_timers_after(monkeypatch, cls: type, *timer_attrs: str) -> Callable[[], None]:
    orig_init = cls.__init__
    instances: list[object] = []

    def _tracking_init(self, *args, **kwargs) -> None:
        orig_init(self, *args, **kwargs)
        instances.append(self)

    monkeypatch.setattr(cls, "__init__", _tracking_init)

    def _finalize() -> None:
        for obj in instances:
            for attr in timer_attrs:
                target = getattr(obj, attr, None)
                if target is None:
                    continue
                target() if callable(target) else target.stop()

    return _finalize
