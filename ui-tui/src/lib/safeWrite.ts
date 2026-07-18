/**
 * Best-effort stderr write that catches stream errors (EIO, EBADF, etc.).
 *
 * Use ONLY in teardown-sensitive paths — signal handlers, uncaughtException
 * reporters, and memory-monitor callbacks — where a closed or broken stream
 * is expected and the write failure must not cascade into a second uncaught
 * exception or unhandled rejection.
 *
 * Normal application output should continue to use process.stderr.write()
 * directly so that real write errors surface visibly.
 */
export function safeStderrWrite(data: string): void {
  try {
    process.stderr.write(data)
  } catch {
    // Stream is broken/closed (EIO, EBADF, etc.) — suppress silently.
    // In teardown the terminal is already gone; logging the write failure
    // to the same broken stream would loop infinitely.
  }
}
