import type { Message, StepProgress, ToolCall } from '../hooks/useChat'

export interface SessionMetrics {
  assistantMessages: number
  toolCalls: number
  pendingTools: number
  approvals: number
}

export interface SessionInsights {
  metrics: SessionMetrics
  activeStep?: StepProgress
  currentTool?: ToolCall
  latestAssistant?: Message
}

export function getSessionInsights(messages: Message[]): SessionInsights {
  const assistantMessages = messages.filter((message) => message.role === 'assistant')

  const metrics: SessionMetrics = {
    assistantMessages: assistantMessages.length,
    toolCalls: assistantMessages.reduce(
      (total, message) => total + (message.toolCalls?.length ?? 0),
      0,
    ),
    pendingTools: assistantMessages.reduce(
      (total, message) => total + (message.toolCalls?.filter((tool) => tool.pending).length ?? 0),
      0,
    ),
    approvals: assistantMessages.reduce(
      (total, message) => total + (message.approvals?.length ?? 0),
      0,
    ),
  }

  const activeStep = assistantMessages
    .flatMap((message) => message.stepProgress ?? [])
    .find((step) => step.status === 'active' || step.status === 'retrying')

  const latestAssistant = [...assistantMessages].reverse().find(Boolean)
  const currentTool = latestAssistant
    ? [...(latestAssistant.toolCalls ?? [])].reverse().find((tool) => tool.pending)
      ?? [...(latestAssistant.toolCalls ?? [])].reverse().find(Boolean)
    : undefined

  return {
    metrics,
    activeStep,
    currentTool,
    latestAssistant,
  }
}
