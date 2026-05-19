"""Tests for the thread-safe iteration budget."""

import threading
import pytest
from app.agent.iteration_budget import IterationBudget


def test_consume_within_budget():
    budget = IterationBudget(max_iterations=5)
    assert budget.consume(1) is True
    assert budget.remaining == 4
    assert budget.consumed == 1


def test_consume_exceeds_budget():
    budget = IterationBudget(max_iterations=2)
    assert budget.consume(1) is True
    assert budget.consume(1) is True
    assert budget.consume(1) is False
    assert budget.remaining == 0


def test_consume_multiple_at_once():
    budget = IterationBudget(max_iterations=10)
    assert budget.consume(5) is True
    assert budget.remaining == 5
    assert budget.consume(6) is False
    assert budget.remaining == 5


def test_refund():
    budget = IterationBudget(max_iterations=10)
    budget.consume(5)
    budget.refund(3)
    assert budget.remaining == 8
    assert budget.consumed == 2


def test_refund_capped_at_max():
    budget = IterationBudget(max_iterations=10)
    budget.consume(2)
    budget.refund(5)
    assert budget.remaining == 10


def test_fork_and_merge():
    parent = IterationBudget(max_iterations=20)
    child = parent.fork(child_budget=5)
    assert parent.remaining == 15
    assert child.remaining == 5

    child.consume(2)
    parent.merge_child(child)
    assert parent.remaining == 18


def test_fork_limited_by_parent():
    parent = IterationBudget(max_iterations=3)
    child = parent.fork(child_budget=10)
    assert child.remaining == 3
    assert parent.remaining == 0


def test_activity_tracking():
    budget = IterationBudget(max_iterations=10)
    assert budget.current_tool is None
    budget.set_current_tool("web_search")
    assert budget.current_tool == "web_search"
    budget.set_current_tool(None)
    assert budget.current_tool is None


def test_stall_detection():
    budget = IterationBudget(max_iterations=10)
    assert budget.is_stalled(timeout=0.001) is False
    budget.consume(1)
    assert budget.is_stalled(timeout=999999) is False


def test_thread_safety():
    budget = IterationBudget(max_iterations=1000)
    errors = []

    def worker():
        for _ in range(100):
            if not budget.consume(1):
                errors.append("Budget exhausted prematurely")

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    assert budget.consumed == 1000
    assert budget.remaining == 0
