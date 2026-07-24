export type GatewayEventName =
  | 'gateway.ready'
  | 'session.info'
  | 'message.start'
  | 'message.delta'
  | 'message.interim'
  | 'message.complete'
  | 'thinking.delta'
  | 'reasoning.delta'
  | 'reasoning.available'
  | 'status.update'
  | 'tool.start'
  | 'tool.progress'
  | 'tool.complete'
  | 'tool.generating'
  | 'clarify.request'
  | 'approval.request'
  | 'sudo.request'
  | 'secret.request'
  | 'background.complete'
  | 'error'
  | 'skin.changed'
  | (string & {})

export interface GatewayEvent<P = unknown> {
  payload?: P
  /** Renderer-side source tag added by the Desktop gateway registry. */
  profile?: string
  session_id?: string
  type: GatewayEventName
}

export type ConnectionState = 'idle' | 'connecting' | 'open' | 'closed' | 'error'
export type GatewayRequestId = number | string

export interface JsonRpcFrame {
  error?: { message?: string }
  id?: GatewayRequestId | null
  method?: string
  params?: GatewayEvent
  result?: unknown
}

export type WebSocketLike = WebSocket

type PendingCall = {
  reject: (error: Error) => void
  resolve: (value: unknown) => void
  timer?: ReturnType<typeof setTimeout>
}

type PendingConnection = {
  reject: (error: Error) => void
  socket: WebSocketLike
}

export interface GatewayClientOptions {
  closedErrorMessage?: string
  connectErrorMessage?: string
  connectTimeoutMs?: number
  createRequestId?: (nextId: number) => GatewayRequestId
  requestIdPrefix?: string
  requestTimeoutMs?: number
  socketFactory?: (url: string) => WebSocketLike
  notConnectedErrorMessage?: string
}

const ANY = '*'
const DEFAULT_REQUEST_TIMEOUT_MS = 120_000
// A reconnect after sleep/wake must not hang forever in 'connecting' (which
// keeps the composer disabled and stuck on "Starting Hermes..."). If the open
// handshake doesn't land in this window, fail to 'error' so callers can retry.
const DEFAULT_CONNECT_TIMEOUT_MS = 15_000

export class JsonRpcGatewayClient {
  private nextId = 0
  private pending = new Map<GatewayRequestId, PendingCall>()
  private pendingConnection: PendingConnection | null = null
  private socket: WebSocketLike | null = null
  private state: ConnectionState = 'idle'
  private readonly eventHandlers = new Map<string, Set<(event: GatewayEvent) => void>>()
  private readonly stateHandlers = new Set<(state: ConnectionState) => void>()
  private readonly options: Required<Omit<GatewayClientOptions, 'socketFactory'>> &
    Pick<GatewayClientOptions, 'socketFactory'>

  constructor(options: GatewayClientOptions = {}) {
    this.options = {
      closedErrorMessage: options.closedErrorMessage ?? 'WebSocket closed',
      connectErrorMessage: options.connectErrorMessage ?? 'WebSocket connection failed',
      connectTimeoutMs: options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS,
      createRequestId: options.createRequestId ?? ((nextId: number) => `${options.requestIdPrefix ?? 'r'}${nextId}`),
      notConnectedErrorMessage: options.notConnectedErrorMessage ?? 'gateway not connected',
      requestIdPrefix: options.requestIdPrefix ?? 'r',
      requestTimeoutMs: options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS,
      socketFactory: options.socketFactory
    }
  }

  get connectionState(): ConnectionState {
    return this.state
  }

  async connect(wsUrl: string): Promise<void> {
    // Refuse garbage; WebSocket coerces non-strings into
    // `ws://<origin>/[object%20Object]` (#68250 stale-emit boot loop).
    const invalidUrl = () => {
      const got = typeof wsUrl === 'string' ? JSON.stringify(wsUrl) : `type "${typeof wsUrl}"`

      return new Error(`gateway connect() requires a ws:// or wss:// URL string, got ${got}`)
    }

    if (typeof wsUrl !== 'string') {
      throw invalidUrl()
    }

    let url: URL
    try {
      url = new URL(wsUrl)
    } catch {
      throw invalidUrl()
    }

    // The WebSocket constructor rejects every fragment, including an empty
    // trailing "#". URL.hash cannot distinguish an absent fragment from an
    // empty one, while the serialized URL preserves the delimiter.
    if ((url.protocol !== 'ws:' && url.protocol !== 'wss:') || url.href.includes('#')) {
      throw invalidUrl()
    }

    if (this.socket?.readyState === WebSocket.OPEN || this.state === 'connecting') {
      return
    }

    const socket = this.options.socketFactory?.(wsUrl) ?? new WebSocket(wsUrl)
    this.socket = socket

    socket.addEventListener('message', message => {
      if (this.socket !== socket) {
        return
      }

      this.handleMessage(message.data)
    })

    const connection = new Promise<void>((resolve, reject) => {
      let settled = false
      let timer: ReturnType<typeof setTimeout> | undefined

      const cleanup = () => {
        if (timer !== undefined) {
          clearTimeout(timer)
        }

        socket.removeEventListener('open', onOpen)
        socket.removeEventListener('error', onError)
        socket.removeEventListener('close', onClose)
        if (this.pendingConnection?.socket === socket) {
          this.pendingConnection = null
        }
      }

      const fail = (error: Error) => {
        if (settled) {
          return
        }

        settled = true
        cleanup()
        reject(error)
      }

      const onOpen = () => {
        if (settled || this.socket !== socket) {
          return
        }

        settled = true
        cleanup()
        this.setState('open')
        resolve()
      }

      const onError = () => {
        if (settled || this.socket !== socket) {
          return
        }

        this.setState('error')
        fail(new Error(this.options.connectErrorMessage))
      }

      const onClose = () => {
        if (settled || this.socket !== socket) {
          return
        }

        fail(new Error(this.options.connectErrorMessage))
      }

      this.pendingConnection = { reject: fail, socket }
      socket.addEventListener('open', onOpen, { once: true })
      socket.addEventListener('error', onError, { once: true })
      socket.addEventListener('close', onClose, { once: true })

      if (this.options.connectTimeoutMs > 0) {
        timer = setTimeout(() => {
          if (settled) {
            return
          }

          fail(new Error(this.options.connectErrorMessage))

          // Drop the half-open socket so the next connect() starts clean
          // instead of short-circuiting on a zombie 'connecting' state.
          if (this.socket === socket) {
            this.socket = null
            try {
              socket.close()
            } catch {
              // ignore
            }
          }

          this.setState('error')
        }, this.options.connectTimeoutMs)
      }
    })

    // Register the persistent lifecycle handler after the one-shot handshake
    // listener. An early close rejects `connection` first, then publishes the
    // durable closed state and clears the socket.
    socket.addEventListener('close', () => {
      if (this.socket !== socket) {
        return
      }

      this.socket = null
      this.setState('closed')
      this.rejectAllPending(new Error(this.options.closedErrorMessage))
    })

    // The pending handshake must own its rejection path before publishing
    // "connecting": state observers run synchronously and may call close().
    // Socket construction still happens first so constructor failures leave
    // the client idle and retryable.
    this.setState('connecting')

    await connection
  }

  close(): void {
    const socket = this.socket

    if (!socket) {
      return
    }

    if (this.pendingConnection?.socket === socket) {
      this.pendingConnection.reject(new Error(this.options.closedErrorMessage))
    }

    try {
      socket.close()
    } finally {
      this.socket = null
      this.setState('closed')
      this.rejectAllPending(new Error(this.options.closedErrorMessage))
    }
  }

  on<P = unknown>(type: GatewayEventName, handler: (event: GatewayEvent<P>) => void): () => void {
    let handlers = this.eventHandlers.get(type)

    if (!handlers) {
      handlers = new Set()
      this.eventHandlers.set(type, handlers)
    }

    handlers.add(handler as (event: GatewayEvent) => void)

    return () => handlers?.delete(handler as (event: GatewayEvent) => void)
  }

  onAny(handler: (event: GatewayEvent) => void): () => void {
    return this.on(ANY as GatewayEventName, handler)
  }

  onEvent(handler: (event: GatewayEvent) => void): () => void {
    return this.onAny(handler)
  }

  onState(handler: (state: ConnectionState) => void): () => void {
    this.stateHandlers.add(handler)
    handler(this.state)

    return () => this.stateHandlers.delete(handler)
  }

  request<T>(
    method: string,
    params: Record<string, unknown> = {},
    timeoutMs = this.options.requestTimeoutMs,
    signal?: AbortSignal
  ): Promise<T> {
    const socket = this.socket

    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return Promise.reject(new Error(this.options.notConnectedErrorMessage))
    }

    if (signal?.aborted) {
      return Promise.reject(new DOMException('Aborted', 'AbortError'))
    }

    const id = this.options.createRequestId(++this.nextId)

    return new Promise<T>((resolve, reject) => {
      let onAbort: (() => void) | undefined

      const detach = () => {
        if (onAbort && signal) {
          signal.removeEventListener('abort', onAbort)
        }
      }

      const pending: PendingCall = {
        resolve: value => {
          detach()
          resolve(value as T)
        },
        reject: error => {
          detach()
          reject(error)
        }
      }

      if (timeoutMs > 0) {
        pending.timer = setTimeout(() => {
          if (this.pending.delete(id)) {
            detach()
            reject(new Error(`request timed out: ${method}`))
          }
        }, timeoutMs)
      }

      // Abort drops the pending call immediately (no dangling resolver/timer);
      // server-side cancellation is a separate cooperative RPC where it matters.
      if (signal) {
        onAbort = () => {
          const call = this.pending.get(id)

          if (call?.timer) {
            clearTimeout(call.timer)
          }

          this.pending.delete(id)
          detach()
          reject(new DOMException('Aborted', 'AbortError'))
        }

        signal.addEventListener('abort', onAbort, { once: true })
      }

      this.pending.set(id, pending)

      try {
        socket.send(
          JSON.stringify({
            jsonrpc: '2.0',
            id,
            method,
            params
          })
        )
      } catch (error) {
        this.clearPending(id)
        detach()
        reject(error instanceof Error ? error : new Error(String(error)))
      }
    })
  }

  private handleMessage(raw: unknown): void {
    const text = typeof raw === 'string' ? raw : String(raw)
    let frame: JsonRpcFrame

    try {
      frame = JSON.parse(text) as JsonRpcFrame
    } catch {
      return
    }

    if (frame.id !== undefined && frame.id !== null) {
      const call = this.pending.get(frame.id)

      if (!call) {
        return
      }

      this.clearPending(frame.id)

      if (frame.error) {
        call.reject(new Error(frame.error.message || 'Hermes RPC failed'))
      } else {
        call.resolve(frame.result)
      }

      return
    }

    if (frame.method === 'event' && frame.params?.type) {
      this.dispatchEvent(frame.params)
    }
  }

  private clearPending(id: GatewayRequestId): void {
    const call = this.pending.get(id)

    if (call?.timer) {
      clearTimeout(call.timer)
    }

    this.pending.delete(id)
  }

  private dispatchEvent(event: GatewayEvent): void {
    for (const handler of this.eventHandlers.get(event.type) ?? []) {
      handler(event)
    }

    for (const handler of this.eventHandlers.get(ANY) ?? []) {
      handler(event)
    }
  }

  private rejectAllPending(error: Error): void {
    for (const [id, call] of this.pending) {
      if (call.timer) {
        clearTimeout(call.timer)
      }

      call.reject(error)
      this.pending.delete(id)
    }
  }

  private setState(state: ConnectionState): void {
    if (this.state === state) {
      return
    }

    this.state = state

    for (const handler of this.stateHandlers) {
      handler(state)
    }
  }
}
