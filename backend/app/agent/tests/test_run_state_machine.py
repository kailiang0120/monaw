from app.agent.run_state_machine import AgentRunState, AgentRunStateMachine


def test_agent_run_state_machine_tracks_core_turn_phases_without_runtime_dependencies():
    machine = AgentRunStateMachine()

    machine.model_call()
    machine.tool_execution()
    machine.waiting_for("approval_required")
    machine.tool_execution()
    machine.finalizing()
    machine.complete()

    assert machine.state == AgentRunState.COMPLETED
    assert [transition.to_state for transition in machine.transitions] == [
        AgentRunState.MODEL_CALL,
        AgentRunState.TOOL_EXECUTION,
        AgentRunState.WAITING_APPROVAL,
        AgentRunState.TOOL_EXECUTION,
        AgentRunState.FINALIZING,
        AgentRunState.COMPLETED,
    ]


def test_agent_run_state_machine_distinguishes_access_waits_and_terminal_states():
    machine = AgentRunStateMachine()

    machine.waiting_for("access_grant_required")
    machine.cancel("user_cancelled")
    machine.complete()

    assert machine.state == AgentRunState.CANCELLED
    assert machine.transitions[-1].reason == "user_cancelled"
    assert AgentRunState.COMPLETED not in [transition.to_state for transition in machine.transitions]
