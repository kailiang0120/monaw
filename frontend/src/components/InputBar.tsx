import { type ClipboardEvent, KeyboardEvent, useEffect, useRef, useState } from 'react'
import { FileText, Loader2, Mic, Paperclip, Send, Shield, Square, Terminal, X } from 'lucide-react'
import { ContextUsageBar } from './ContextUsageBar'
import { Dropdown } from './Dropdown'
import { formatReasoningEffort, providerOptions, reasoningEffortsForProvider } from '../features/settings/settingsConfig'
import { fetchContextUsage } from '../lib/api/conversations'
import { transcribeSpeech } from '../lib/api/speechToText'
import { uploadAttachment } from '../lib/api/uploads'
import type { AgentSettings, ContextUsage, ModelOptions, UploadedAttachment } from '../lib/api/types'

type ApprovalMode = AgentSettings['permissions']['mode']
type ModelSelection = Pick<AgentSettings['llm'], 'provider' | 'model_name' | 'reasoning_effort'>

const APPROVAL_OPTIONS: Array<{ value: ApprovalMode; label: string }> = [
  { value: 'default', label: 'Default' },
  { value: 'full_access', label: 'Full Access' },
  { value: 'auto_review', label: 'Auto Review' },
  { value: 'custom', label: 'Custom' },
]
const VOICE_AUTO_STOP_MS = 60000
const VOICE_SEGMENT_MS = 3000
const VOICE_MIME_TYPES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/mp4',
  'audio/ogg;codecs=opus',
]
const WAVEFORM_RATIOS = [0.35, 0.65, 0.9, 1, 0.85, 0.55, 0.75, 0.45, 0.95, 0.6]
const SLASH_COMMANDS = [
  {
    command: '/compact',
    label: 'Compact context',
    description: 'Summarize this chat and use it as future context.',
  },
]

function ComposerAttachment({ file, onRemove, disabled }: { file: File; onRemove: () => void; disabled: boolean }) {
  const isImage = file.type.startsWith('image/') || (!file.type && /\.(apng|avif|bmp|gif|jpe?g|png|svg|webp)$/i.test(file.name))
  const [previewUrl, setPreviewUrl] = useState('')
  const [previewFailed, setPreviewFailed] = useState(false)

  useEffect(() => {
    if (!isImage) return
    const url = URL.createObjectURL(file)
    setPreviewUrl(url)
    setPreviewFailed(false)
    return () => URL.revokeObjectURL(url)
  }, [file, isImage])

  return (
    <div
      className={isImage
        ? 'relative h-24 w-28 shrink-0 overflow-hidden rounded-xl border border-white/[0.08] bg-white/[0.03]'
        : 'inline-flex max-w-[220px] items-center gap-1.5 rounded-lg border border-white/[0.07] bg-white/[0.03] py-1 pl-2 pr-1 text-[11px] text-neutral-400'}
    >
      {isImage ? (
        previewFailed ? (
          <span className="flex h-full items-center justify-center px-2 text-center text-[11px] text-neutral-500">Preview unavailable</span>
        ) : previewUrl ? (
          <img src={previewUrl} alt={`Preview of ${file.name}`} className="h-full w-full object-contain" onError={() => setPreviewFailed(true)} />
        ) : null
      ) : (
        <>
          <FileText size={12} className="shrink-0 text-neutral-600" />
          <span className="truncate" title={file.name}>{file.name}</span>
        </>
      )}
      <button
        type="button"
        onClick={onRemove}
        className={isImage
          ? 'absolute right-1 top-1 flex h-6 w-6 items-center justify-center rounded-full border border-[var(--st-border-strong)] bg-[var(--st-surface)] text-[var(--st-text)] shadow-sm transition-colors hover:bg-[var(--st-surface-hover)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--st-accent)] disabled:opacity-50'
          : 'flex h-6 w-6 shrink-0 items-center justify-center rounded text-neutral-500 hover:bg-white/[0.06] hover:text-neutral-200 disabled:opacity-50'}
        aria-label={`Remove ${file.name}`}
        disabled={disabled}
      >
        <X size={14} />
      </button>
    </div>
  )
}

interface Props {
  onSend: (text: string, attachments?: UploadedAttachment[]) => void
  onSteer?: (text: string, attachments?: UploadedAttachment[]) => Promise<void>
  steeringConversationId?: string | null
  onStop: () => void
  isStreaming: boolean
  conversationId: string | null
  contextRefreshKey: number
  usageRefreshKey?: number
  disabled?: boolean
  disabledReason?: string
  approvalMode: ApprovalMode
  modelSelection: ModelSelection
  modelOptions: ModelOptions
  hasGoogleKey?: boolean
  modelSelectionDisabled?: boolean
  modelSelectionError?: string
  onModelSelectionChange: (selection: ModelSelection) => void
  focusRequestKey?: number
  approvalModeDisabled?: boolean
  onApprovalModeChange: (mode: ApprovalMode) => void
}

export function InputBar({
  onSend,
  onSteer,
  steeringConversationId,
  onStop,
  isStreaming,
  conversationId,
  contextRefreshKey,
  usageRefreshKey = 0,
  disabled,
  disabledReason,
  approvalMode,
  modelSelection,
  modelOptions,
  hasGoogleKey = false,
  modelSelectionDisabled,
  modelSelectionError,
  onModelSelectionChange,
  focusRequestKey = 0,
  approvalModeDisabled,
  onApprovalModeChange,
}: Props) {
  const [value, setValue] = useState('')
  const [selectedFiles, setSelectedFiles] = useState<File[]>([])
  const [uploading, setUploading] = useState(false)
  const [recording, setRecording] = useState(false)
  const [transcribing, setTranscribing] = useState(false)
  const [voiceLevel, setVoiceLevel] = useState(0)
  const [uploadError, setUploadError] = useState('')
  const [contextUsage, setContextUsage] = useState<ContextUsage | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const previousFocusRequestKeyRef = useRef(focusRequestKey)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const mediaRecorderRef = useRef<MediaRecorder | null>(null)
  const recordingSegmentChunksRef = useRef<Blob[]>([])
  const recordingStreamRef = useRef<MediaStream | null>(null)
  const recordingTimeoutRef = useRef<number | null>(null)
  const recordingSegmentTimeoutRef = useRef<number | null>(null)
  const voiceAudioContextRef = useRef<AudioContext | null>(null)
  const voiceLevelFrameRef = useRef<number | null>(null)
  const voiceLevelLastLogRef = useRef(0)
  const voiceFinishedRef = useRef(false)
  const voiceHadTranscriptRef = useRef(false)
  const voiceProcessingErrorRef = useRef(false)
  const voicePendingChunksRef = useRef(0)
  const voiceRecordedBytesRef = useRef(0)
  const voiceSegmentIndexRef = useRef(0)
  const voiceNextTranscriptIndexRef = useRef(0)
  const voiceTranscriptQueueRef = useRef<Map<number, string>>(new Map())
  const voiceStopRequestedRef = useRef(false)
  const modelChoices = providerOptions(modelOptions)
    .filter((provider) => provider.id !== 'gemini' || hasGoogleKey)
    .flatMap((provider) =>
      provider.models.map((model) => ({
        value: `${provider.id}:${model}`,
        label: `${provider.label} · ${model}`,
      })),
    )
  const selectedModelAvailable = modelChoices.some(
    (choice) => choice.value === `${modelSelection.provider}:${modelSelection.model_name}`,
  )
  const effortChoices = reasoningEffortsForProvider(modelSelection.provider, modelSelection.model_name)
  const settingsSaving = modelSelectionDisabled || approvalModeDisabled
  const canSteer = Boolean(isStreaming && onSteer && steeringConversationId)
  const placeholder = disabled && disabledReason
    ? disabledReason
    : isStreaming ? 'Enter to steer · Shift+Enter for newline' : 'Enter to send · Shift+Enter for newline'
  const slashQuery = value.startsWith('/') && !value.includes('\n') && !value.includes(' ')
    ? value.slice(1).toLowerCase()
    : ''
  const slashCommandMatches = value.startsWith('/') && !value.includes('\n') && !value.includes(' ')
    ? SLASH_COMMANDS.filter((item) => {
      const command = item.command.slice(1).toLowerCase()
      return command.startsWith(slashQuery) || item.label.toLowerCase().includes(slashQuery)
    })
    : []
  const showSlashCommands = !recording && !isStreaming && !disabled && !settingsSaving && slashCommandMatches.length > 0

  useEffect(() => {
    if (previousFocusRequestKeyRef.current === focusRequestKey) return
    previousFocusRequestKeyRef.current = focusRequestKey
    textareaRef.current?.focus()
  }, [focusRequestKey])

  useEffect(() => () => {
    if (recordingTimeoutRef.current) window.clearTimeout(recordingTimeoutRef.current)
    if (recordingSegmentTimeoutRef.current) window.clearTimeout(recordingSegmentTimeoutRef.current)
    if (voiceLevelFrameRef.current) window.cancelAnimationFrame(voiceLevelFrameRef.current)
    void voiceAudioContextRef.current?.close()
    const recorder = mediaRecorderRef.current
    if (recorder) recorder.onstop = null
    if (recorder?.state === 'recording') recorder.stop()
    recordingStreamRef.current?.getTracks().forEach((track) => track.stop())
  }, [])

  useEffect(() => {
    let cancelled = false
    const controller = new AbortController()
    if (!conversationId) {
      setContextUsage(null)
      return
    }
    if (isStreaming) {
      return () => {
        cancelled = true
        controller.abort()
      }
    }
    setContextUsage(null)
    const loadUsage = async () => {
      try {
        const usage = await fetchContextUsage(conversationId, controller.signal)
        if (!cancelled) setContextUsage(usage)
      } catch {
        if (!cancelled) setContextUsage(null)
      }
    }
    void loadUsage()
    return () => {
      cancelled = true
      controller.abort()
    }
  }, [conversationId, contextRefreshKey, isStreaming, usageRefreshKey])

  const handleSend = async () => {
    const trimmed = value.trim()
    const message = trimmed || (selectedFiles.length ? 'Please review the attached file(s).' : '')
    if (!message || !selectedModelAvailable || settingsSaving || (isStreaming && !canSteer) || disabled || uploading || recording || transcribing) return
    setUploadError('')
    setUploading(true)
    try {
      const attachments = selectedFiles.length
        ? await Promise.all(selectedFiles.map((file) => uploadAttachment(file, isStreaming ? steeringConversationId ?? conversationId : conversationId)))
        : []
      if (isStreaming && onSteer) await onSteer(message, attachments)
      else onSend(message, attachments)
      setValue('')
      setSelectedFiles([])
      if (fileInputRef.current) fileInputRef.current.value = ''
      if (textareaRef.current) textareaRef.current.style.height = 'auto'
    } catch (error) {
      setUploadError(error instanceof Error ? error.message : 'Upload failed')
    } finally {
      setUploading(false)
    }
  }

  const chooseSlashCommand = (command: string, submit = false) => {
    setValue(command)
    window.setTimeout(() => {
      textareaRef.current?.focus()
      handleInput()
    }, 0)
    if (submit) {
      window.setTimeout(() => {
        void handleSendCommand(command)
      }, 0)
    }
  }

  const handleSendCommand = async (command: string) => {
    if (settingsSaving || isStreaming || disabled || uploading || recording || transcribing) return
    setUploadError('')
    onSend(command, [])
    setValue('')
    setSelectedFiles([])
    if (fileInputRef.current) fileInputRef.current.value = ''
    if (textareaRef.current) textareaRef.current.style.height = 'auto'
  }

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (showSlashCommands && slashCommandMatches[0]) {
      if (e.key === 'Tab' || (e.key === 'Enter' && value.trim() !== slashCommandMatches[0].command)) {
        e.preventDefault()
        chooseSlashCommand(slashCommandMatches[0].command, e.key === 'Enter')
        return
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        setValue('')
        return
      }
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      void handleSend()
    }
  }

  const handleInput = () => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 180)}px`
  }

  const addFiles = (files: FileList | File[] | null) => {
    if (!files?.length) return
    setUploadError('')
    setSelectedFiles((current) => [...current, ...Array.from(files)].slice(0, 8))
  }

  const handlePaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    // Files and items describe the same clipboard payload. Prefer the file list
    // and only use items as a fallback so images aren't attached twice.
    const files = Array.from(event.clipboardData.files)
    if (!files.length) {
      for (const item of Array.from(event.clipboardData.items)) {
        if (item.kind !== 'file') continue
        const file = item.getAsFile()
        if (file) files.push(file)
      }
    }
    if (!files.length) return
    event.preventDefault()
    if ((isStreaming && !canSteer) || disabled || uploading || recording || transcribing) return
    addFiles(files)
  }

  const removeFile = (index: number) => {
    setSelectedFiles((current) => current.filter((_, itemIndex) => itemIndex !== index))
  }

  const focusAndResizeTextarea = () => {
    window.setTimeout(() => {
      textareaRef.current?.focus()
      handleInput()
    }, 0)
  }

  const stopRecordingStream = () => {
    if (recordingTimeoutRef.current) {
      window.clearTimeout(recordingTimeoutRef.current)
      recordingTimeoutRef.current = null
    }
    if (recordingSegmentTimeoutRef.current) {
      window.clearTimeout(recordingSegmentTimeoutRef.current)
      recordingSegmentTimeoutRef.current = null
    }
    recordingStreamRef.current?.getTracks().forEach((track) => track.stop())
    recordingStreamRef.current = null
    if (voiceLevelFrameRef.current) {
      window.cancelAnimationFrame(voiceLevelFrameRef.current)
      voiceLevelFrameRef.current = null
    }
    void voiceAudioContextRef.current?.close()
    voiceAudioContextRef.current = null
    setVoiceLevel(0)
  }

  const appendVoiceTranscript = (transcript: string) => {
    const text = transcript.trim()
    if (!text) return
    voiceHadTranscriptRef.current = true
    setValue((current) => {
      const trimmed = current.trimEnd()
      return trimmed ? `${trimmed} ${text}` : text
    })
    focusAndResizeTextarea()
  }

  const flushVoiceTranscriptQueue = () => {
    const orderedParts: string[] = []
    let nextIndex = voiceNextTranscriptIndexRef.current
    while (voiceTranscriptQueueRef.current.has(nextIndex)) {
      const transcript = voiceTranscriptQueueRef.current.get(nextIndex)?.trim() || ''
      voiceTranscriptQueueRef.current.delete(nextIndex)
      if (transcript) orderedParts.push(transcript)
      nextIndex += 1
    }
    voiceNextTranscriptIndexRef.current = nextIndex
    if (orderedParts.length > 0) appendVoiceTranscript(orderedParts.join(' '))
  }

  const settleVoiceProcessing = () => {
    if (voicePendingChunksRef.current > 0) return
    setTranscribing(false)
    if (!voiceFinishedRef.current) return
    if (!voiceProcessingErrorRef.current && !voiceHadTranscriptRef.current) {
      const sizeKb = Math.round(voiceRecordedBytesRef.current / 1024)
      setUploadError(sizeKb > 0
        ? `No speech detected. Audio was recorded (${sizeKb} KB); check backend.log for speech-to-text details.`
        : 'No audio was recorded.')
    }
    mediaRecorderRef.current = null
  }

  const transcribeVoiceSegment = async (blob: Blob, phase: 'segment' | 'final', sequence: number) => {
    if (blob.size <= 0) return
    voiceRecordedBytesRef.current += blob.size
    voicePendingChunksRef.current += 1
    setTranscribing(true)
    try {
      console.info('[voice-input] transcribing chunk', {
        phase,
        sequence,
        blobSize: blob.size,
        mimeType: blob.type || mediaRecorderRef.current?.mimeType || 'audio/webm',
      })
      const transcript = (await transcribeSpeech(blob)).text.trim()
      voiceTranscriptQueueRef.current.set(sequence, transcript)
      flushVoiceTranscriptQueue()
      if (!transcript) {
        console.info('[voice-input] empty transcript chunk', { phase, blobSize: blob.size })
      }
    } catch (error) {
      voiceProcessingErrorRef.current = true
      voiceTranscriptQueueRef.current.set(sequence, '')
      flushVoiceTranscriptQueue()
      setUploadError(error instanceof Error ? error.message : 'Voice transcription failed')
    } finally {
      voicePendingChunksRef.current = Math.max(0, voicePendingChunksRef.current - 1)
      settleVoiceProcessing()
    }
  }

  const finishVoiceRecording = () => {
    voiceFinishedRef.current = true
    stopRecordingStream()
    console.info('[voice-input] recording stopped', {
      recordedBytes: voiceRecordedBytesRef.current,
      pendingChunks: voicePendingChunksRef.current,
      hadTranscript: voiceHadTranscriptRef.current,
    })
    settleVoiceProcessing()
  }

  const startVoiceLevelMonitor = (stream: MediaStream) => {
    const AudioContextCtor = window.AudioContext || (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
    if (!AudioContextCtor) return
    const audioContext = new AudioContextCtor()
    const source = audioContext.createMediaStreamSource(stream)
    const analyser = audioContext.createAnalyser()
    analyser.fftSize = 1024
    source.connect(analyser)
    voiceAudioContextRef.current = audioContext
    const samples = new Uint8Array(analyser.fftSize)
    const tick = () => {
      analyser.getByteTimeDomainData(samples)
      let sumSquares = 0
      let peak = 0
      for (const sample of samples) {
        const normalized = (sample - 128) / 128
        sumSquares += normalized * normalized
        peak = Math.max(peak, Math.abs(normalized))
      }
      const rms = Math.sqrt(sumSquares / samples.length)
      setVoiceLevel(Math.min(1, peak))
      const now = Date.now()
      if (now - voiceLevelLastLogRef.current > 2000) {
        voiceLevelLastLogRef.current = now
        console.info('[voice-input] mic level', {
          rms: Number(rms.toFixed(4)),
          peak: Number(peak.toFixed(4)),
          state: audioContext.state,
        })
      }
      voiceLevelFrameRef.current = window.requestAnimationFrame(tick)
    }
    void audioContext.resume()
    tick()
  }

  const stopVoiceRecording = () => {
    voiceStopRequestedRef.current = true
    if (recordingSegmentTimeoutRef.current) {
      window.clearTimeout(recordingSegmentTimeoutRef.current)
      recordingSegmentTimeoutRef.current = null
    }
    const recorder = mediaRecorderRef.current
    if (recorder?.state === 'recording') {
      recorder.stop()
    } else {
      setRecording(false)
      finishVoiceRecording()
    }
  }

  const startVoiceSegmentRecorder = (stream: MediaStream, mimeType?: string) => {
    if (voiceStopRequestedRef.current) return
    const recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream)
    mediaRecorderRef.current = recorder
    recordingSegmentChunksRef.current = []
    recorder.ondataavailable = (event) => {
      console.debug('[voice-input] segment data', {
        size: event.data.size,
        type: event.data.type || recorder.mimeType || 'unknown',
      })
      if (event.data.size > 0) recordingSegmentChunksRef.current.push(event.data)
    }
    recorder.onstop = () => {
      if (recordingSegmentTimeoutRef.current) {
        window.clearTimeout(recordingSegmentTimeoutRef.current)
        recordingSegmentTimeoutRef.current = null
      }
      const chunks = recordingSegmentChunksRef.current
      recordingSegmentChunksRef.current = []
      const blob = new Blob(chunks, { type: recorder.mimeType || mimeType || 'audio/webm' })
      const phase = voiceStopRequestedRef.current ? 'final' : 'segment'
      if (blob.size > 0) {
        const sequence = voiceSegmentIndexRef.current
        voiceSegmentIndexRef.current += 1
        void transcribeVoiceSegment(blob, phase, sequence)
      }
      if (voiceStopRequestedRef.current) {
        setRecording(false)
        finishVoiceRecording()
        return
      }
      startVoiceSegmentRecorder(stream, mimeType)
    }
    recorder.onerror = () => {
      voiceStopRequestedRef.current = true
      setRecording(false)
      stopRecordingStream()
      setUploadError('Voice recording failed.')
    }
    recorder.start()
    recordingSegmentTimeoutRef.current = window.setTimeout(() => {
      if (recorder.state === 'recording') recorder.stop()
    }, VOICE_SEGMENT_MS)
  }

  const toggleVoiceRecording = async () => {
    if (recording) {
      stopVoiceRecording()
      return
    }
    if (isStreaming || disabled || uploading || transcribing) return
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      setUploadError('Voice input is not available in this browser.')
      return
    }
    try {
      setUploadError('')
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      recordingStreamRef.current = stream
      const [audioTrack] = stream.getAudioTracks()
      const permissionStatus = await navigator.permissions?.query?.({ name: 'microphone' as PermissionName }).catch(() => null)
      console.info('[voice-input] microphone stream granted', {
        permission: permissionStatus?.state || 'unknown',
        trackLabel: audioTrack?.label || 'unknown',
        trackEnabled: audioTrack?.enabled,
        trackMuted: audioTrack?.muted,
        trackReadyState: audioTrack?.readyState,
        settings: audioTrack?.getSettings?.(),
      })
      const supportedMimeType = VOICE_MIME_TYPES.find((mimeType) => MediaRecorder.isTypeSupported(mimeType))
      voiceFinishedRef.current = false
      voiceHadTranscriptRef.current = false
      voiceProcessingErrorRef.current = false
      voicePendingChunksRef.current = 0
      voiceRecordedBytesRef.current = 0
      voiceSegmentIndexRef.current = 0
      voiceNextTranscriptIndexRef.current = 0
      voiceTranscriptQueueRef.current.clear()
      voiceStopRequestedRef.current = false
      console.info('[voice-input] recording started', {
        mimeType: supportedMimeType || 'browser-default',
      })
      setRecording(true)
      startVoiceLevelMonitor(stream)
      startVoiceSegmentRecorder(stream, supportedMimeType)
      recordingTimeoutRef.current = window.setTimeout(() => {
        stopVoiceRecording()
      }, VOICE_AUTO_STOP_MS)
    } catch (error) {
      stopRecordingStream()
      const message = error instanceof Error ? error.message : ''
      const permissionDenied = error instanceof DOMException && ['NotAllowedError', 'SecurityError', 'PermissionDeniedError'].includes(error.name)
      console.error('[voice-input] microphone access failed', {
        name: error instanceof Error ? error.name : '',
        message,
      })
      setUploadError(permissionDenied
        ? 'Microphone access was denied. Enable microphone permission for Monaw in Windows privacy settings, then restart the app.'
        : message || 'Microphone permission was not granted.')
    }
  }

  return (
    <footer className="bg-[#11100f] px-4 pb-4 pt-2">
      <div className="mx-auto max-w-3xl">
        {/* Unified dock panel */}
        <div className="panel relative rounded-2xl transition-colors focus-within:border-accent/40">
          {showSlashCommands && (
            <div className="panel absolute bottom-full left-0 right-0 z-30 mb-2 overflow-hidden rounded-xl p-1">
              {slashCommandMatches.map((item) => (
                <button
                  key={item.command}
                  type="button"
                  onMouseDown={(event) => {
                    event.preventDefault()
                    chooseSlashCommand(item.command)
                  }}
                  className="flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left transition-colors hover:bg-accent/10 focus:bg-accent/10 focus:outline-none"
                >
                  <span className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-accent/25 bg-accent/10 text-accent-light">
                    <Terminal size={13} />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block font-mono text-xs text-neutral-100">{item.command}</span>
                    <span className="block truncate text-[11px] text-neutral-500">{item.description}</span>
                  </span>
                  <span className="rounded-md border border-accent/20 bg-white/[0.03] px-1.5 py-0.5 text-[10px] text-neutral-500">
                    Enter
                  </span>
                </button>
              ))}
            </div>
          )}
          {selectedFiles.length > 0 && (
            <div className="flex flex-wrap items-center gap-2 px-4 pt-3" role="group" aria-label="Attachments">
              {selectedFiles.map((file, index) => (
                <ComposerAttachment
                  key={`${file.name}-${file.size}-${index}`}
                  file={file}
                  onRemove={() => removeFile(index)}
                  disabled={uploading}
                />
              ))}
            </div>
          )}
          {/* Textarea / waveform row */}
          <div className="px-4 pt-3">
            <input
              ref={fileInputRef}
              type="file"
              multiple
              className="hidden"
              accept="image/*,.pdf,.txt,.md,.csv,.json,.docx,.xlsx"
              onChange={(event) => addFiles(event.target.files)}
            />
            {recording ? (
              <div className="flex min-h-9 items-center gap-3">
                {/* Live waveform bars */}
                <div className="flex shrink-0 items-end gap-[3px]" style={{ height: 22 }}>
                  {WAVEFORM_RATIOS.map((ratio, i) => (
                    <span
                      key={i}
                      className="w-[3px] rounded-full bg-red-400/70 transition-all duration-75"
                      style={{ height: Math.max(2, Math.round((voiceLevel > 0.04 ? voiceLevel : 0.14) * ratio * 18 + 2)) }}
                    />
                  ))}
                </div>
                <span className="text-sm text-neutral-500">
                  {voiceLevel > 0.04 ? 'Listening…' : 'Recording…'}
                </span>
              </div>
            ) : (
              <textarea
                ref={textareaRef}
                value={value}
                onChange={(e) => setValue(e.target.value)}
                onKeyDown={handleKeyDown}
                onPaste={handlePaste}
                onInput={handleInput}
                aria-label="Message"
                placeholder={placeholder}
                rows={1}
                disabled={disabled || uploading}
                className="chat-composer-input w-full min-h-9 max-h-44 resize-none bg-transparent text-sm leading-relaxed outline-none disabled:opacity-50"
              />
            )}
          </div>

          {(uploadError || (!recording && transcribing)) && (
            <div className="mx-3 mt-2 flex flex-wrap items-center gap-2 border-t border-white/[0.06] pt-2">
              {!recording && transcribing && (
                <span className="inline-flex items-center gap-1.5 rounded-lg border border-accent/20 bg-accent/10 px-2 py-1 text-[11px] text-accent-light">
                  <Loader2 size={11} className="animate-spin" />
                  Transcribing voice...
                </span>
              )}
              {uploadError && <span className="text-[11px] text-red-300">{uploadError}</span>}
            </div>
          )}

          {/* Bottom action row */}
          <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 px-3 pb-3 pt-1.5">
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                disabled={(isStreaming && !canSteer) || disabled || uploading || recording || transcribing}
                className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-white/[0.07] bg-white/[0.03] text-neutral-500 outline-none transition-colors hover:border-white/[0.12] hover:text-neutral-200 focus:border-accent/50 disabled:opacity-50"
                aria-label="Attach files"
                title="Attach files or paste images and files into the message"
              >
                <Paperclip size={12} />
              </button>

              <button
                type="button"
                onClick={() => { void toggleVoiceRecording() }}
                disabled={isStreaming || disabled || uploading || (!recording && transcribing)}
                className={`inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border outline-none transition-colors focus:border-accent/50 disabled:opacity-50 ${
                  recording
                    ? 'border-red-400/30 bg-red-500/15 text-red-300 hover:bg-red-500/25'
                    : 'border-white/[0.07] bg-white/[0.03] text-neutral-500 hover:border-white/[0.12] hover:text-neutral-200'
                }`}
                aria-label={recording ? 'Stop voice input' : 'Start voice input'}
                title={recording ? 'Stop voice input' : 'Start voice input'}
              >
                {transcribing ? <Loader2 size={12} className="animate-spin" /> : recording ? <Square size={12} fill="currentColor" /> : <Mic size={12} />}
              </button>

              {/* Permission dropdown */}
              <Dropdown<ApprovalMode>
                ariaLabel="Permission mode"
                value={approvalMode}
                options={APPROVAL_OPTIONS}
                onChange={onApprovalModeChange}
                disabled={approvalModeDisabled}
                size="sm"
                icon={<Shield size={11} className="shrink-0" />}
              />
            </div>

            <div className="flex min-w-0 items-center justify-end gap-2">
              {!recording && (
                <>
                  <Dropdown<string>
                    ariaLabel="Model"
                    value={`${modelSelection.provider}:${modelSelection.model_name}`}
                    options={modelChoices}
                    placeholder="Choose a model"
                    onChange={(selection) => {
                      const separator = selection.indexOf(':')
                      const provider = selection.slice(0, separator) as ModelSelection['provider']
                      const model_name = selection.slice(separator + 1)
                      onModelSelectionChange({ ...modelSelection, provider, model_name })
                    }}
                    disabled={modelSelectionDisabled}
                    size="sm"
                    className="w-[170px] max-w-[42vw]"
                  />
                  <Dropdown<ModelSelection['reasoning_effort']>
                    ariaLabel="Reasoning effort"
                    value={modelSelection.reasoning_effort}
                    options={effortChoices.map((effort) => ({ value: effort, label: formatReasoningEffort(effort) }))}
                    onChange={(reasoning_effort) => onModelSelectionChange({ ...modelSelection, reasoning_effort })}
                    disabled={modelSelectionDisabled}
                    size="sm"
                    className="w-[105px]"
                  />
                </>
              )}

              {/* Context usage ring */}
              <ContextUsageBar usage={contextUsage} />

              {/* Send / Stop */}
              {isStreaming && (
                <button
                  type="button"
                  onClick={onStop}
                  aria-label="Stop response"
                  title="Stop response"
                  className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-red-500/15 text-red-300 ring-1 ring-red-400/20 hover:bg-red-500/25"
                >
                  <Square size={12} fill="currentColor" />
                </button>
              )}
              <button
                type="button"
                onClick={() => { void handleSend() }}
                disabled={(!value.trim() && selectedFiles.length === 0) || !selectedModelAvailable || settingsSaving || (isStreaming && !canSteer) || disabled || uploading || recording || transcribing}
                className="h-7 w-7 shrink-0 rounded-lg primary-button"
                aria-label={isStreaming ? 'Steer message' : 'Send message'}
                title={isStreaming ? 'Steer the running task' : 'Send message'}
              >
                {uploading ? <Loader2 size={12} className="animate-spin" /> : <Send size={12} />}
              </button>
            </div>
          </div>
          {modelSelectionError && <div role="alert" className="px-3 pb-2 text-[11px] text-red-300">{modelSelectionError}</div>}
          {!selectedModelAvailable && (
            <div role="status" className="px-3 pb-2 text-[11px] text-amber-300">
              {modelSelection.provider === 'gemini' && !hasGoogleKey
                ? 'Add a Google API key in Settings or choose another model.'
                : 'Choose an available model.'}
            </div>
          )}
        </div>
      </div>
    </footer>
  )
}
