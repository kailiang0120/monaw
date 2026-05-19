import { useCallback, useEffect, useState } from 'react'
import { Check, ShieldAlert, X } from 'lucide-react'
import {
  approveTicket,
  fetchPendingApprovals,
  rejectTicket,
} from '../lib/api/approvals'
import type { ApprovalTicket } from '../lib/api/types'

interface Props {
  conversationId: string | null
  isStreaming: boolean
}

export function ApprovalToast({ conversationId, isStreaming }: Props) {
  const [toast, setToast] = useState<ApprovalTicket | null>(null)
  const [seenIds, setSeenIds] = useState<Set<string>>(new Set())
  const [acting, setActing] = useState(false)

  const poll = useCallback(async () => {
    if (isStreaming) return
    try {
      const pending = await fetchPendingApprovals(conversationId ?? undefined)
      const unseen = pending.find((t) => !seenIds.has(t.id))
      if (unseen && !toast) {
        setToast(unseen)
        setSeenIds((prev) => new Set(prev).add(unseen.id))
        if ('Notification' in window && Notification.permission === 'granted') {
          new Notification('Approval Required', {
            body: unseen.action_description,
            tag: `approval-${unseen.id}`,
          })
        }
      }
    } catch {
      // Approval polling should not interrupt the chat UI.
    }
  }, [conversationId, isStreaming, seenIds, toast])

  useEffect(() => {
    if ('Notification' in window && Notification.permission === 'default') {
      Notification.requestPermission()
    }
  }, [])

  useEffect(() => {
    if (isStreaming) {
      setToast(null)
      return
    }
    poll()
    const iv = setInterval(poll, 2500)
    return () => clearInterval(iv)
  }, [isStreaming, poll])

  const handleApprove = async () => {
    if (!toast) return
    setActing(true)
    try {
      await approveTicket(toast.id)
    } catch {
      // Keep UX non-blocking; the backend will continue to expose pending items.
    }
    setToast(null)
    setActing(false)
  }

  const handleReject = async () => {
    if (!toast) return
    setActing(true)
    try {
      await rejectTicket(toast.id)
    } catch {
      // Keep UX non-blocking; the backend will continue to expose pending items.
    }
    setToast(null)
    setActing(false)
  }

  if (!toast) return null

  return (
    <div className="fixed right-4 top-4 z-[60] w-80 animate-fade-in">
      <div className="rounded-2xl border border-amber-400/25 bg-[#171615]/95 p-4 shadow-2xl shadow-black/30 backdrop-blur-md">
        <div className="mb-2 flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <ShieldAlert size={16} className="shrink-0 text-amber-300" />
            <span className="text-xs font-semibold text-amber-200">Approval required</span>
          </div>
          <span className="status-pill border-amber-400/20 bg-amber-400/10 text-amber-200">
            {toast.risk_level || 'review'}
          </span>
        </div>
        <p className="mb-1 text-[11px] leading-relaxed text-neutral-200">
          {toast.action_description}
        </p>
        <p className="mb-3 text-[10px] leading-relaxed text-neutral-500">{toast.reason}</p>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={handleApprove}
            disabled={acting}
            className="flex flex-1 items-center justify-center gap-1 rounded-lg bg-emerald-500 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-emerald-400 disabled:opacity-50"
          >
            <Check size={12} /> Approve
          </button>
          <button
            type="button"
            onClick={handleReject}
            disabled={acting}
            className="ghost-button flex-1 rounded-lg px-3 py-1.5 text-xs font-medium disabled:opacity-50"
          >
            <X size={12} /> Reject
          </button>
        </div>
      </div>
    </div>
  )
}
